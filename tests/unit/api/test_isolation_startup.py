"""Startup writes the rules that name proxy addresses once the proxies are up (#554, #568).

The proxies get new addresses at every start, and the paused sandboxes kept
from the previous process can reach only the addresses the chain allows, so
the chain is rewritten right after ``proxy_mgr.start`` rather than at the
first sandbox. A failure there is logged, not fatal: ``create()`` writes the
chain again and refuses every sandbox while it cannot.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.api.app import create_app, lifespan
from embry0.execution.runtime.isolation import IsolationError
from embry0.storage.database import DatabasePool
from tests.fixtures.postgres import postgres_unavailable


class _StopLifespan(BaseException):
    """Sentinel past the call under test. BaseException: startup's ``except Exception`` must not eat it."""


async def test_startup_writes_the_chain_after_the_proxies_and_survives_a_failure(monkeypatch):
    import asyncpg

    from embry0.config import Embry0Config
    from embry0.execution.docker_client import DockerClient

    database_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://embry0:embry0@localhost:5432/embry0_unit_api_test",
    )
    try:
        conn = await asyncpg.connect(database_url)
        await conn.close()
    except (OSError, asyncpg.PostgresError) as exc:
        postgres_unavailable(exc)

    order: list[str] = []

    async def _proxy_start(self, **kwargs):
        order.append("proxies started")

    async def _refresh_proxy_policy():
        # The isolation chain and the egress exceptions (#568), in SandboxManager.
        order.append("chain written")
        raise IsolationError("no proxy is running on 'sandbox-restricted'")

    sandbox_mgr = MagicMock()
    sandbox_mgr.refresh_proxy_policy = _refresh_proxy_policy

    async def _clean_orphans(*args, **kwargs):
        order.append("orphans cleaned")
        raise _StopLifespan

    monkeypatch.setattr("embry0.execution.sandbox_manager.SandboxManager", lambda *a, **kw: sandbox_mgr)
    monkeypatch.setattr("embry0.execution.proxy.manager.ProxyManager.start", _proxy_start)
    monkeypatch.setattr("embry0.api.app._clean_orphaned_sandboxes", _clean_orphans)
    # Everything else on the way needs a live DinD; none of it is under test.
    monkeypatch.setattr("embry0.execution.docker_client.DockerClient", lambda **kw: AsyncMock(spec=DockerClient))
    monkeypatch.setattr("embry0.execution.image_manager.SandboxImageManager", lambda *a, **kw: AsyncMock())
    monkeypatch.setattr("embry0.execution.image_manager.ContainerReaper", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("embry0.execution.runtime.egress.EgressEnforcer", lambda *a, **kw: AsyncMock())

    pools: list[DatabasePool] = []
    real_pool = DatabasePool

    def _recording_pool(url):
        pool = real_pool(url)
        pools.append(pool)
        return pool

    monkeypatch.setattr("embry0.api.app.DatabasePool", _recording_pool)

    config = Embry0Config(
        _env_file=None,
        database_url=database_url,
        auth_dev_mode=True,
        webhook_dev_mode=True,
        api_key="",
        github_webhook_secret="test-secret",
    )
    app = create_app(config)
    try:
        with pytest.raises(_StopLifespan):
            async with lifespan(app):
                pass
    finally:
        for pool in pools:
            await pool.close()

    assert order == ["proxies started", "chain written", "orphans cleaned"], (
        f"startup must write the chain right after the proxies start, and carry on when it cannot: {order}"
    )
