"""/health/ready with several watched stacks (#378).

`watcher` stays the single value alerting already reads — the WORST across
stacks, so a stuck stack cannot hide behind a busy one — and `watcher_stacks`
says which one is which.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config


class _OkDb:
    """A database that answers, so these assertions are about the watcher."""

    pool = object()

    async def fetchval(self, *_args: Any) -> int:
        return 1


def _app(services: dict[str, str] | None, *, enabled: bool = True) -> Any:
    config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True, watcher_enabled=enabled)
    app = create_app(config)
    app.state.db = _OkDb()
    if services is not None:
        app.state.watcher_services = {
            repo: SimpleNamespace(health=state, _repo=repo) for repo, state in services.items()
        }
    return app


async def _ready(app: Any) -> dict[str, Any]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return (await client.get("/api/v1/health/ready")).json()


async def test_every_stack_healthy_reads_ok() -> None:
    body = await _ready(_app({"acme/backend": "ok", "example-org/embry0": "ok"}))
    assert body["checks"]["watcher"] == "ok"
    assert body["checks"]["watcher_stacks"] == {"acme/backend": "ok", "example-org/embry0": "ok"}


async def test_one_broken_stack_degrades_the_endpoint_and_is_named() -> None:
    """The point: a second stack must not be able to hide a fault."""
    body = await _ready(_app({"acme/backend": "ok", "example-org/embry0": "invalid_logql"}))
    assert body["status"] == "degraded"
    assert body["checks"]["watcher"] == "invalid_logql"
    assert body["checks"]["watcher_stacks"]["example-org/embry0"] == "invalid_logql"


async def test_unverified_does_not_degrade_readiness() -> None:
    """Loki has not answered yet — no evidence of a fault (#270)."""
    body = await _ready(_app({"a/b": "ok", "c/d": "unverified"}))
    assert body["checks"]["watcher"] == "unverified"
    assert body["status"] == "ok"


async def test_a_disabled_watcher_is_not_reported() -> None:
    body = await _ready(_app({"a/b": "invalid_logql"}, enabled=False))
    assert "watcher" not in body["checks"]
    assert body["status"] == "ok"


@pytest.mark.parametrize("services", [{}, None])
async def test_no_stacks_reports_no_watcher(services: Any) -> None:
    body = await _ready(_app(services))
    assert "watcher" not in body["checks"]
