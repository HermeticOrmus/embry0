"""Sandbox listing endpoint — ops visibility into running + stale containers."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/sandboxes/active")
async def list_active_sandboxes(request: Request, include_stopped: bool = False) -> dict[str, object]:
    """List sandbox-* containers and their ages.

    Lists containers through the app's runtime (``docker ps``), filtered on the
    ``sandbox-`` name prefix. Returns a list of ``{name, status, running_for}``
    dicts. Ops can cross-reference this with ``ContainerReaper.max_age_hours``
    to spot stale sandboxes before the reaper culls them.

    Returns an empty list if the runtime is unavailable or the command
    fails — this endpoint is strictly observability and must not 5xx the
    dashboard just because the docker socket is momentarily unreachable.
    """
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        return {"containers": [], "count": 0}

    try:
        found = await runtime.list_containers(name="sandbox-", include_stopped=include_stopped, timeout=10)
    except Exception:
        logger.warning("sandbox_list_failed", exc_info=True)
        return {"containers": [], "count": 0}

    containers = [{"name": c.name, "status": c.status, "running_for": c.running_for} for c in found]
    return {"containers": containers, "count": len(containers)}
