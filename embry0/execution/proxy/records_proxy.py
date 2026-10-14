"""Records proxy — read-only repo records for the agents of one job's repository.

Runs on the sandbox-restricted network. An agent inside a job may read the
records of the job's own repository (RCAs, runbooks, specs; docs/records.md)
and nothing else. This proxy is the only path from a sandbox to them:

- per-sandbox bearer enrollment through the shared hardened registry
  (:mod:`embry0.execution.proxy.enrollment`), as every credential proxy;
- the **repository comes from the enrollment, never from the request**. Each
  enrollment carries ``allowed_repos``: the job's repository, or ``[]`` for a
  job without one. The sandbox asks for ``GET /records`` or
  ``GET /records/{slug}`` and the proxy builds the upstream path from the
  enrolled repository, so no request names another repository's records. An
  enrollment without the field is refused; one with an empty scope gets 403
  on every call;
- two GET routes, a query-parameter allowlist and a slug pattern; anything
  else is 400 or 404 and never reaches the orchestrator;
- it holds a read-scope orchestrator service key (context-proxy precedent)
  and injects it upstream; the key never enters the sandbox.

No embry0 import beyond the proxy package: this module ships in the proxy
image, which carries no orchestrator code, so the repository-path rule of
``embry0/api/schemas/repo_path.py`` is restated here.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from aiohttp import web

from embry0.execution.proxy.enrollment import EnrollRejected, check_bearer, register_admin_routes

logger = structlog.get_logger(__name__)

SCOPE_FIELD = "allowed_repos"

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
_REPO_RE = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT})+\Z")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")

_LIST_PARAMS = frozenset({"kind", "q", "limit"})
_GET_PARAMS = frozenset({"revision"})


def normalize_records_repo(repo: str) -> str | None:
    """``repo`` when it is a repository path the records routes accept, else None."""
    return repo if _REPO_RE.match(repo) else None


class _RepoScope:
    """Enroll-body ``allowed_repos`` -> the one repository this sandbox may read."""

    def parse(self, app: web.Application, body: dict[str, Any]) -> str | None:
        if SCOPE_FIELD not in body:
            raise EnrollRejected(f"{SCOPE_FIELD} is required")
        requested = body[SCOPE_FIELD]
        if not isinstance(requested, list) or not all(isinstance(r, str) for r in requested):
            raise EnrollRejected(f"{SCOPE_FIELD} must be a list of repository paths")
        if len(requested) > 1:
            raise EnrollRejected(f"{SCOPE_FIELD} names at most one repository")
        if not requested:
            return None
        repo = normalize_records_repo(requested[0])
        if repo is None:
            raise EnrollRejected(f"{SCOPE_FIELD} names an invalid repository path")
        return repo

    def apply(self, app: web.Application, sandbox_id: str, parsed: str | None) -> dict[str, Any]:
        app["repo_by_id"][sandbox_id] = parsed
        return {SCOPE_FIELD: [parsed] if parsed else []}


def create_records_proxy_app(upstream_url: str, orchestrator_api_key: str, admin_token: str) -> web.Application:
    """Create the records proxy app.

    Args:
        upstream_url: orchestrator base URL (IP-resolved by ProxyManager).
        orchestrator_api_key: read-scope service key, injected upstream.
        admin_token: shared secret for the ``/admin/*`` endpoints.
    """
    if not upstream_url:
        raise ValueError("upstream_url is required")
    if not orchestrator_api_key:
        raise ValueError("orchestrator_api_key is required")
    app = web.Application()
    app["upstream_url"] = upstream_url.rstrip("/")
    app["orchestrator_api_key"] = orchestrator_api_key
    # Cleared with each enrollment by the registry's on_unenroll hook.
    app["repo_by_id"] = {}
    register_admin_routes(
        app,
        proxy_name="records_proxy",
        admin_token=admin_token,
        on_unenroll=_forget_sandbox,
        enroll_extension=_RepoScope(),
    )
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/health", _health_handler)
    app.router.add_get("/records", _list_handler)
    app.router.add_get("/records/{slug}", _get_handler)
    return app


def _forget_sandbox(app: web.Application, sandbox_id: str) -> None:
    app["repo_by_id"].pop(sandbox_id, None)


async def _on_startup(app: web.Application) -> None:
    app["http_client"] = httpx.AsyncClient(timeout=30.0)


async def _on_cleanup(app: web.Application) -> None:
    client = app.get("http_client")
    if client:
        await client.aclose()


async def _health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "proxy": "records", "enrolled": len(request.app["enrollment"])})


def _scoped_repo(request: web.Request) -> tuple[str | None, web.Response | None]:
    sandbox_id = check_bearer(request, "records_proxy")
    if sandbox_id is None:
        return None, web.json_response({"error": "unauthorized"}, status=401)
    repo: str | None = request.app["repo_by_id"].get(sandbox_id)
    if not repo:
        logger.warning("records_proxy_no_scope", sandbox_id=sandbox_id)
        return None, web.json_response({"error": "this job has no repository whose records it may read"}, status=403)
    return repo, None


def _unknown_params(request: web.Request, allowed: frozenset[str]) -> web.Response | None:
    unknown = set(request.query.keys()) - allowed
    if not unknown:
        return None
    return web.json_response(
        {"error": f"unsupported parameter(s): {', '.join(sorted(unknown))}", "allowed_params": sorted(allowed)},
        status=400,
    )


async def _list_handler(request: web.Request) -> web.Response:
    repo, denied = _scoped_repo(request)
    if denied is not None:
        return denied
    assert repo is not None
    refused = _unknown_params(request, _LIST_PARAMS)
    if refused is not None:
        return refused
    params = {k: request.query[k] for k in _LIST_PARAMS if request.query.get(k)}
    return await _forward(request, repo, "", params)


async def _get_handler(request: web.Request) -> web.Response:
    repo, denied = _scoped_repo(request)
    if denied is not None:
        return denied
    assert repo is not None
    refused = _unknown_params(request, _GET_PARAMS)
    if refused is not None:
        return refused
    slug = request.match_info["slug"]
    if not _SLUG_RE.match(slug):
        return web.json_response({"error": "invalid record slug"}, status=400)
    params = {"revision": request.query["revision"]} if request.query.get("revision") else {}
    return await _forward(request, repo, slug, params)


async def _forward(request: web.Request, repo: str, slug: str, params: dict[str, str]) -> web.Response:
    """GET the enrolled repository's records upstream. Nothing from the sandbox request but
    the checked slug and parameters is forwarded."""
    path = f"/api/v1/repos/{quote(repo, safe='/')}/records"
    if slug:
        path += f"/{slug}"
    headers = {"Authorization": f"Bearer {request.app['orchestrator_api_key']}"}
    client: httpx.AsyncClient = request.app["http_client"]
    logger.info("records_proxy_read", repo=repo, slug=slug or None)
    try:
        upstream = await client.get(f"{request.app['upstream_url']}{path}", params=params, headers=headers)
    except httpx.ConnectError:
        return web.json_response({"error": "Failed to connect to upstream API"}, status=502)
    except httpx.TimeoutException:
        return web.json_response({"error": "Upstream API timeout"}, status=504)
    return web.Response(
        status=upstream.status_code,
        body=upstream.content,
        content_type=upstream.headers.get("content-type", "application/json").split(";")[0],
    )
