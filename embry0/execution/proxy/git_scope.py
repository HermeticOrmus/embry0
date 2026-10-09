"""Per-sandbox scope for the git-proxy's smart-HTTP forwarding (#521).

The git-proxy used to answer the sandbox's credential helper with the forge
token itself, so anything holding the sandbox bearer got the token, with its
full scope on every repository it can reach. Now the sandbox's git uses the
proxy as its remote and the proxy adds the credential to each upstream request
(ADR 0007). This module decides which requests it forwards:

* **Grant.** Each enrollment carries ``allowed_repos`` (the job's repository,
  as for the forge API proxies in #520), ``git_upstream`` (the forge's base URL
  the sandbox would otherwise clone from) and ``git_push``. The orchestrator
  sends ``[]`` when a job has no repository.
* **Routes.** ``/git/<repo>[.git]/info/refs?service=<svc>`` (GET),
  ``/git/<repo>[.git]/git-upload-pack`` and ``.../git-receive-pack`` (POST).
  Nothing else: no dumb HTTP, no LFS (its batch API hands the client further
  URLs and authorization headers that would have to be rewritten), no query
  other than the one ``info/refs`` needs.
* **Strict path.** Every segment is ``[A-Za-z0-9_.-]``, so there is no
  percent-encoding to interpret; dot segments and empty segments are refused.
  The upstream URL is built from the checked segments.
* **Push.** receive-pack, both its advertisement and the push itself, needs
  ``git_push``.
* **Fail closed.** An enrollment without ``allowed_repos`` (an orchestrator
  older than the proxy image) is refused every git request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import structlog
from aiohttp import web

from embry0.execution.proxy.enrollment import EnrollRejected
from embry0.execution.proxy.forge_scope import SCOPE_FIELD, normalize_gitlab_project, parse_allowed_repos

__all__ = [
    "GIT_ROUTE_PREFIX",
    "PUSH_FIELD",
    "RECEIVE_PACK",
    "UPLOAD_PACK",
    "UPSTREAM_FIELD",
    "GitGrant",
    "GitScope",
    "GitVerdict",
    "git_verdict",
    "normalize_git_repo",
    "normalize_upstream",
]

logger = structlog.get_logger(__name__)

UPSTREAM_FIELD = "git_upstream"
PUSH_FIELD = "git_push"
GIT_ROUTE_PREFIX = "/git/"

UPLOAD_PACK = "git-upload-pack"
RECEIVE_PACK = "git-receive-pack"
_SERVICES = (UPLOAD_PACK, RECEIVE_PACK)

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
_UPSTREAM_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.~-]{1,255}$")


@dataclass(frozen=True)
class GitGrant:
    """What one enrolled sandbox may do through the git-proxy."""

    upstream: str
    repos: frozenset[str]
    push: bool


@dataclass(frozen=True)
class GitVerdict:
    """Whether one git request may be forwarded; ``upstream_url`` is set only when it may."""

    allowed: bool
    reason: str = ""
    repo: str = ""
    service: str = ""
    upstream_url: str = ""


def _deny(reason: str) -> GitVerdict:
    return GitVerdict(allowed=False, reason=reason)


def normalize_git_repo(value: str) -> str | None:
    """Repository path lower-cased, or None if malformed.

    A GitHub ``owner/name`` is the two-segment case of a GitLab project path,
    and one git-proxy serves both forges, so the GitLab rule covers both.
    """
    return normalize_gitlab_project(value)


def normalize_upstream(value: str) -> str | None:
    """``scheme://host[:port][/path]`` without a trailing slash, or None.

    Only ``http`` and ``https``; no credentials, query or fragment in the URL,
    and a path of plain segments (a self-managed forge under a relative root).
    """
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return None
    if parts.query or parts.fragment or not parts.hostname or not _HOST_RE.fullmatch(parts.hostname):
        return None
    path = parts.path.rstrip("/")
    if path:
        segments = path[1:].split("/")
        if not all(_UPSTREAM_PATH_SEGMENT_RE.fullmatch(s) and s not in (".", "..") for s in segments):
            return None
    netloc = parts.hostname if port is None else f"{parts.hostname}:{port}"
    return f"{parts.scheme}://{netloc}{path}"


def git_verdict(method: str, raw_path: str, raw_query: str, grant: GitGrant | None) -> GitVerdict:
    """Decide one request on a ``/git/...`` route against the sandbox's grant."""
    if grant is None:
        return _deny("unscoped_enrollment")
    if not raw_path.startswith(GIT_ROUTE_PREFIX):
        return _deny("path_not_allowed")
    segments = raw_path[len(GIT_ROUTE_PREFIX) :].split("/")
    if not all(_SEGMENT_RE.fullmatch(s) and s not in (".", "..") for s in segments):
        return _deny("malformed_path")

    if segments[-2:] == ["info", "refs"]:
        repo_segments, suffix, expected_method = segments[:-2], "info/refs", "GET"
        service = raw_query[len("service=") :] if raw_query.startswith("service=") else ""
        if service not in _SERVICES:
            return _deny("query_not_allowed")
        query = f"?service={service}"
    elif segments[-1] in _SERVICES:
        repo_segments, suffix, expected_method = segments[:-1], segments[-1], "POST"
        service = segments[-1]
        if raw_query:
            return _deny("query_not_allowed")
        query = ""
    else:
        return _deny("path_not_allowed")
    if method != expected_method:
        return _deny("method_not_allowed")

    if repo_segments and repo_segments[-1].endswith(".git"):
        repo_segments = [*repo_segments[:-1], repo_segments[-1][: -len(".git")]]
    repo = normalize_git_repo("/".join(repo_segments))
    if repo is None:
        return _deny("malformed_path")
    if repo not in grant.repos:
        return _deny("repo_out_of_scope")
    if service == RECEIVE_PACK and not grant.push:
        return _deny("push_not_allowed")

    upstream_url = f"{grant.upstream}/{'/'.join(repo_segments)}.git/{suffix}{query}"
    return GitVerdict(allowed=True, repo=repo, service=service, upstream_url=upstream_url)


class GitScope:
    """Enroll-body ``allowed_repos`` / ``git_upstream`` / ``git_push`` → one :class:`GitGrant`.

    Implements :class:`~embry0.execution.proxy.enrollment.EnrollExtension`.
    Grants live at ``app["git_grant_by_id"]`` and end with their enrollment
    (:meth:`forget`); a sandbox with none is refused every git request. The response echoes the stored grant, which is how the
    orchestrator tells this proxy from an image that predates forwarding.
    """

    def __init__(self, app: web.Application, *, proxy_name: str) -> None:
        self._proxy_name = proxy_name
        app["git_grant_by_id"] = {}

    def parse(self, app: web.Application, body: dict[str, Any]) -> GitGrant | None:
        repos = parse_allowed_repos(body, normalize_git_repo)
        if repos is None:
            return None
        raw_upstream = body.get(UPSTREAM_FIELD)
        upstream = ""
        if raw_upstream is not None:
            normalized = normalize_upstream(raw_upstream)
            if normalized is None:
                raise EnrollRejected(f"{UPSTREAM_FIELD} {str(raw_upstream)[:200]!r} is not an http(s) base URL")
            upstream = normalized
        if repos and not upstream:
            raise EnrollRejected(f"{SCOPE_FIELD} names a repository but there is no {UPSTREAM_FIELD}")
        push = body.get(PUSH_FIELD, False)
        if not isinstance(push, bool):
            raise EnrollRejected(f"{PUSH_FIELD} must be true or false")
        return GitGrant(upstream=upstream, repos=repos, push=push)

    def apply(self, app: web.Application, sandbox_id: str, parsed: GitGrant | None) -> dict[str, Any]:
        grants: dict[str, GitGrant] = app["git_grant_by_id"]
        if parsed is None:
            grants.pop(sandbox_id, None)
            logger.warning(
                f"{self._proxy_name}_enrollment_unscoped",
                sandbox_id=sandbox_id,
                detail=f"no {SCOPE_FIELD} in the enrollment; every git request from this sandbox will be refused",
            )
            return {}
        grants[sandbox_id] = parsed
        return {SCOPE_FIELD: sorted(parsed.repos), UPSTREAM_FIELD: parsed.upstream, PUSH_FIELD: parsed.push}

    def forget(self, app: web.Application, sandbox_id: str) -> None:
        """``on_unenroll`` hook: the registry runs it whenever an enrollment ends (#536).

        Revoked, expired or replaced, a grant never outlives its enrollment, so
        the map is bounded by the registry's cap.
        """
        app["git_grant_by_id"].pop(sandbox_id, None)
