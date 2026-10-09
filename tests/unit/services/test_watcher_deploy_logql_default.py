"""An unset WATCHER_DEPLOY_LOGQL switches deploy markers off cleanly (#527).

The setting has no code default any more. Unset, the watcher must not ask
Loki for deploy lines, not probe a deploy query, and not log a fault on
every tick — the briefing simply has no deploy section and no case gets a
deploy anchor.
"""

import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import structlog

from embry0.config import Embry0Config
from tests.unit.services.test_watcher import _loki, _service

WATCHED = '{compose_project="acme"}'
_ENV = {
    "WATCHER_ENABLED": "true",
    "WATCHER_LOKI_URL": "http://loki.test:3100",
    "WATCHER_LOGQL": WATCHED,
    "WATCHER_REPO": "acme/widgets",
}


def _shipped_config() -> Embry0Config:
    """The real settings class, with nothing set for the deploy query."""
    with patch.dict(os.environ, _ENV, clear=True):
        return Embry0Config(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_an_unset_deploy_query_asks_loki_nothing_and_logs_no_fault():
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["query"])
        return httpx.Response(200, json={"data": {"result": []}})

    svc = _service(config=_shipped_config())
    svc._http = _loki(handler)
    svc._deploy_sync = MagicMock(lookback_hours=AsyncMock(return_value=24), record=AsyncMock())
    now = datetime.now(UTC)

    with structlog.testing.capture_logs() as logs:
        checks = await svc.validate_queries()
        markers = await svc._fetch_deploy_markers(now)
        await svc._sync_deploys(now)

    assert queries == [WATCHED], f"only WATCHER_LOGQL may reach Loki, got {queries!r}"
    assert set(checks) == {"WATCHER_LOGQL"} and svc.health == "ok", f"{checks!r} / {svc.health}"
    assert markers == [], "an unset deploy query must yield no deploy markers"
    svc._deploy_sync.record.assert_not_awaited()
    faults = [e for e in logs if e["log_level"] in ("warning", "error", "critical")]
    assert faults == [], f"an unset deploy query is not a fault: {faults!r}"
