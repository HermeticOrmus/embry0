"""Reconcile must not erase tracker keys from ``watcher_runs`` (#278).

``release_identifier`` used to free a cap slot by NULLing
``linear_issue_identifier``. That kept the cap honest but destroyed the only
link from a proposal to its ticket: 9 of the first 30 proposals lost their
Linear id this way. The slot is now released with a ``released_at`` stamp,
so the audit row keeps its ticket id.
"""

from datetime import UTC, datetime, timedelta

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS
from embry0.storage.repositories.watcher_runs import WatcherRunsRepository


def test_migration_68_adds_released_at_idempotently() -> None:
    match = [sql for v, _, sql in MIGRATIONS if v == 68]
    assert len(match) == 1
    assert "ADD COLUMN IF NOT EXISTS released_at" in match[0]


async def _propose(repo: WatcherRunsRepository, identifier: str) -> str:
    now = datetime.now(UTC)
    return await repo.create(
        window_start=now - timedelta(hours=1),
        window_end=now,
        action="proposed",
        detail=f"draft {identifier}",
        fingerprint=f"fp-{identifier}",
        tracker="linear",
        tracker_key=identifier,
    )


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_release_keeps_the_ticket_id_and_frees_the_slot(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    run_id = await _propose(repo, "ENG-1097")
    await _propose(repo, "ENG-1098")
    assert await repo.open_proposal_count() == 2

    assert await repo.release_ticket("linear", "ENG-1097") == 1

    row = await db_with_migrations.fetchrow("SELECT * FROM watcher_runs WHERE id = $1", run_id)
    assert row["tracker_key"] == "ENG-1097"
    assert row["released_at"] is not None
    assert await repo.open_proposal_count() == 1
    assert await repo.open_proposal_tickets() == [{"tracker": "linear", "key": "ENG-1098"}]


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_release_is_idempotent(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    run_id = await _propose(repo, "ENG-1097")
    assert await repo.release_ticket("linear", "ENG-1097") == 1
    first = await db_with_migrations.fetchval("SELECT released_at FROM watcher_runs WHERE id = $1", run_id)

    # A second reconcile pass finds nothing left to release and must not
    # restamp the row.
    assert await repo.release_ticket("linear", "ENG-1097") == 0
    again = await db_with_migrations.fetchval("SELECT released_at FROM watcher_runs WHERE id = $1", run_id)
    assert again == first
