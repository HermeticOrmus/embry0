"""GitLab project discovery for the Add-repo flow (#169).

Mirrors ``GET /api/v1/github/discoverable-repos``: registry credentials for
gitlab, then the env tokens, minus projects already registered. Each row
names the granting credential. A group access token still covers its subgroup
tree; the fan-out is for several such accounts.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from embry0.execution.gitlab_client import GitLabClient
from embry0.execution.tls import load_ca_pem
from embry0.services.credentials.discovery import ForgeCredentialEntry, forge_credential_entries

logger = structlog.get_logger(__name__)
router = APIRouter()

# Same shape as the GitHub discovery cache: the visible project set changes on
# human timescales, and the Add-repo panel is opened repeatedly while an
# operator works through a list.
_CACHE_TTL_SECONDS = 60.0
_CACHE: dict[str, Any] = {"at": 0.0, "by_path": {}, "failed": []}

# Runaway guard, matching the GitHub side. Logged rather than silently applied.
_MAX_PAGES = 10


class GitLabProject(BaseModel):
    full_name: str
    description: str | None = None
    private: bool
    html_url: str
    default_branch: str
    open_issues_count: int = 0
    credential_id: str | None = None
    credential_label: str = ""


class DiscoverableProjectsResponse(BaseModel):
    projects: list[GitLabProject]
    cached: bool
    configured: bool
    failed: list[str] = Field(
        default_factory=list,
        description="Labels of credentials whose listing raised. Never a token.",
    )


def _to_project(raw: dict[str, Any], entry: ForgeCredentialEntry) -> GitLabProject:
    return GitLabProject(
        full_name=str(raw.get("path_with_namespace") or ""),
        description=raw.get("description"),
        # GitLab has three visibilities; only `public` is genuinely open.
        private=str(raw.get("visibility") or "private") != "public",
        html_url=str(raw.get("web_url") or ""),
        default_branch=str(raw.get("default_branch") or "main"),
        open_issues_count=int(raw.get("open_issues_count") or 0),
        credential_id=entry.credential_id,
        credential_label=entry.label,
    )


@router.get("/gitlab/discoverable-projects", response_model=DiscoverableProjectsResponse)
async def list_discoverable_projects(
    request: Request,
    refresh: bool = Query(default=False),
) -> DiscoverableProjectsResponse:
    """Projects the configured GitLab accounts can see, minus registered ones."""
    config = request.app.state.config
    # The registry or the environment may hold the credential; an app built
    # without the lifespan has no resolver, so fall back to the env token.
    resolver = getattr(request.app.state, "credential_resolver", None)
    if resolver is not None:
        configured = await resolver.has_forge_credential("gitlab")
    else:
        configured = bool(getattr(config, "gitlab_token", "") or "")
    if not configured:
        # Not an error: a GitHub-only deployment is the normal case. The UI
        # hides the GitLab option rather than showing an empty failing list.
        return DiscoverableProjectsResponse(projects=[], cached=False, configured=False, failed=[])

    now = time.monotonic()
    cached = not refresh and bool(_CACHE["by_path"]) and now - _CACHE["at"] < _CACHE_TTL_SECONDS
    if not cached:
        entries = await forge_credential_entries(
            getattr(request.app.state, "credentials_repo", None),
            getattr(request.app.state, "secrets_provider", None),
            config,
            "gitlab",
        )
        by_path, failed = await _projects_for_entries(entries, config)
        _CACHE["at"] = time.monotonic()
        _CACHE["by_path"] = by_path
        _CACHE["failed"] = failed

    registered = {row["repo"] for row in await request.app.state.repos_repo.list_all(include_inactive=True)}
    projects: list[GitLabProject] = []
    for path in sorted(_CACHE["by_path"]):
        if path in registered:
            continue
        entry, raw = _CACHE["by_path"][path]
        projects.append(_to_project(raw, entry))
    failed = list(_CACHE["failed"])

    logger.info(
        "gitlab_projects_listed",
        count=len(projects),
        registered=len(registered),
        cached=cached,
        failed=failed,
    )
    return DiscoverableProjectsResponse(projects=projects, cached=cached, configured=True, failed=failed)


async def _projects_for_entries(
    entries: list[ForgeCredentialEntry], config: Any
) -> tuple[dict[str, tuple[ForgeCredentialEntry, dict[str, Any]]], list[str]]:
    """One client per entry. The entry is the token — no resolver.

    First path wins. A listing that raises is recorded by label and skipped.
    Raise only when every entry raised, matching the GitHub discovery route.
    ``strict=True`` makes a non-200 raise; the default paginator would return
    the pages it already had and the dead token would look like an empty list.
    """
    if not entries:
        return {}, []

    ca_pem = load_ca_pem(str(getattr(config, "gitlab_ca_bundle", "") or ""))
    gitlab_url = str(getattr(config, "gitlab_url", "") or "")
    by_path: dict[str, tuple[ForgeCredentialEntry, dict[str, Any]]] = {}
    failed: list[str] = []
    last_error: httpx.HTTPStatusError | None = None
    succeeded = False
    for entry in entries:
        try:
            client = GitLabClient(default_token=entry.token, gitlab_url=gitlab_url, ca_pem=ca_pem)
            raw_projects = await client.paginate(
                "/projects",
                params={"membership": "true", "order_by": "last_activity_at", "simple": "false"},
                per_page=100,
                max_pages=_MAX_PAGES,
                strict=True,
            )
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "gitlab_projects_fetch_failed",
                status=exc.response.status_code,
                label=entry.label,
            )
            failed.append(entry.label)
            last_error = exc
            continue
        succeeded = True
        shown = entry.without_token()
        for raw in raw_projects:
            if not isinstance(raw, dict) or not raw.get("path_with_namespace"):
                continue
            path = str(raw["path_with_namespace"])
            if path not in by_path:
                by_path[path] = (shown, raw)

    if not succeeded:
        status = last_error.response.status_code if last_error else 400
        raise HTTPException(status_code=status, detail=f"GitLab API returned {status}") from last_error
    return by_path, failed
