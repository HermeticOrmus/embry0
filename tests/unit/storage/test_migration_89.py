"""Migration 89 — backfill jobs.finished_at on terminal rows written without one (#345)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS

TERMINAL = ("completed", "partial", "failed", "cancelled", "expired", "pr_merged", "pr_closed")
LIVE = ("pending", "running", "awaiting_input", "paused")


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1
    return match[0][2]


def test_migration_89_is_present_and_the_list_is_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 89 in versions
    assert versions == list(range(1, len(versions) + 1))


async def _seed(db: DatabasePool, job_id: str, status: str, *, updated_at: str, finished_at: str | None) -> None:
    await db.execute("ALTER TABLE jobs DISABLE TRIGGER trg_jobs_updated_at")
    await db.execute(
        "INSERT INTO jobs (job_id, repo, task, status, updated_at, finished_at) "
        "VALUES ($1, 'o/r', 't', $2, $3::text::timestamptz, $4::text::timestamptz)",
        job_id,
        status,
        updated_at,
        finished_at,
    )
    await db.execute("ALTER TABLE jobs ENABLE TRIGGER trg_jobs_updated_at")


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_89_stamps_every_terminal_row_and_only_those(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    for status in TERMINAL + LIVE:
        await _seed(db, f"j-{status}", status, updated_at="2026-08-01T10:00:00Z", finished_at=None)
    await _seed(db, "j-stamped", "failed", updated_at="2026-08-01T10:00:00Z", finished_at="2026-07-01T09:00:00Z")

    for _ in range(2):  # the second pass must be a no-op
        await db.execute(_sql_for(89))

    rows = {r["job_id"]: r for r in await db.fetch("SELECT job_id, finished_at, updated_at FROM jobs")}
    for status in TERMINAL:
        row = rows[f"j-{status}"]
        assert row["finished_at"] == row["updated_at"], f"a {status} row was not backfilled from updated_at"
    for status in LIVE:
        assert rows[f"j-{status}"]["finished_at"] is None, f"a live {status} row was given a finish time"
    assert rows["j-stamped"]["finished_at"].isoformat() == "2026-07-01T09:00:00+00:00", "an existing value moved"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_89_does_not_touch_jobs_updated_at_and_rearms_the_trigger(
    db_with_migrations: DatabasePool,
) -> None:
    db = db_with_migrations
    await _seed(db, "j", "failed", updated_at="2026-08-01T10:00:00Z", finished_at=None)
    before = await db.fetchval("SELECT updated_at FROM jobs WHERE job_id = 'j'")

    await db.execute(_sql_for(89))

    assert await db.fetchval("SELECT updated_at FROM jobs WHERE job_id = 'j'") == before
    assert await db.fetchval("SELECT tgenabled::text FROM pg_trigger WHERE tgname = 'trg_jobs_updated_at'") == "O"
