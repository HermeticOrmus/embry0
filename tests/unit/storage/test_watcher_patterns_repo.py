"""WatcherPatternsRepository (RAV-1138) — upsert semantics, pure-mock."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.storage.repositories.watcher_patterns import WatcherPatternsRepository


def _repo():
    db = MagicMock()
    db.execute = AsyncMock()
    db.fetch = AsyncMock(return_value=[])
    return WatcherPatternsRepository(db), db


@pytest.mark.asyncio
async def test_upsert_conflicts_on_fingerprint_and_bumps_occurrences():
    repo, db = _repo()
    await repo.upsert("onlyoffice 404", "healthcheck probes 404 by design", repo="acme/backend")
    sql, *args = db.execute.await_args.args
    assert "ON CONFLICT (fingerprint, repo)" in sql
    assert "occurrences + 1" in sql
    assert "last_seen = NOW()" in sql
    assert args == ["onlyoffice 404", "acme/backend", "healthcheck probes 404 by design"]


@pytest.mark.asyncio
async def test_list_recent_orders_by_last_seen_and_caps():
    repo, db = _repo()
    await repo.list_recent(limit=10)
    sql, *args = db.fetch.await_args.args
    assert "ORDER BY last_seen DESC" in sql
    assert args == [10, None]
