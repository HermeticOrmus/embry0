"""The two durable anchors the watcher loop schedules from (#335)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from embry0.storage.repositories.watcher_runs import WatcherRunsRepository

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

_NOW = datetime(2026, 9, 20, 5, 0, tzinfo=UTC)


async def test_last_tick_at_is_the_newest_run_whatever_its_action(db_with_migrations):
    repo = WatcherRunsRepository(db_with_migrations)
    assert await repo.last_tick_at() is None
    for action, age_minutes in [("no_issue", 90), ("error", 20), ("skipped_backlog", 50)]:
        run_id = await repo.create(window_start=_NOW, window_end=_NOW, action=action)
        await db_with_migrations.execute(
            "UPDATE watcher_runs SET created_at = $2 WHERE id = $1", run_id, _NOW - timedelta(minutes=age_minutes)
        )
    assert await repo.last_tick_at() == _NOW - timedelta(minutes=20)


async def test_tick_attempt_is_one_mark_that_only_moves_forward(db_with_migrations):
    repo = WatcherRunsRepository(db_with_migrations)
    assert await repo.last_tick_attempt_at() is None
    await repo.record_tick_attempt(_NOW)
    await repo.record_tick_attempt(_NOW + timedelta(hours=1))
    # A late writer with an older clock reading must not rewind the mark.
    await repo.record_tick_attempt(_NOW - timedelta(hours=3))
    assert await repo.last_tick_attempt_at() == _NOW + timedelta(hours=1)
    assert await db_with_migrations.fetchval("SELECT COUNT(*) FROM watcher_tick_attempts") == 1


async def test_an_attempt_leaves_no_watcher_run(db_with_migrations):
    """The run list is the audit trail of finished ticks; attempts stay out of it."""
    repo = WatcherRunsRepository(db_with_migrations)
    await repo.record_tick_attempt(_NOW)
    assert await repo.list_recent() == []
    assert await repo.last_tick_at() is None


async def test_a_naive_attempt_time_is_refused(db_with_migrations):
    repo = WatcherRunsRepository(db_with_migrations)
    with pytest.raises(ValueError):
        await repo.record_tick_attempt(datetime(2026, 9, 20, 5, 0))
