"""Migration 73 — jobs.delivery_state, seeded from what the DB can prove (#279, #280)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS

PR = "https://github.com/acme/widgets/pull/1"


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1
    return match[0][2]


def test_migration_73_is_present_and_the_list_is_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 73 in versions
    assert versions == list(range(1, len(versions) + 1))


async def _reset(db: DatabasePool) -> None:
    """Put the seeded rows back to the pre-migration state (every row `none`)."""
    await db.execute("ALTER TABLE jobs DISABLE TRIGGER trg_jobs_updated_at")
    await db.execute("UPDATE jobs SET delivery_state = 'none', delivery_state_at = NULL")
    await db.execute("ALTER TABLE jobs ENABLE TRIGGER trg_jobs_updated_at")


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_73_seeds_delivery_from_status_and_pr_url(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    rows = [
        ("j-merged", "pr_merged", PR, "merged"),
        ("j-closed", "pr_closed", PR, "closed"),
        ("j-failed-pr", "failed", PR, "pr_open"),
        ("j-done-pr", "completed", PR, "pr_open"),
        ("j-failed", "failed", None, "none"),
        ("j-empty-url", "completed", "", "none"),
    ]
    for job_id, status, pr_url, _ in rows:
        await db.execute(
            "INSERT INTO jobs (job_id, repo, task, status, pr_url) VALUES ($1, 'acme/widgets', 't', $2, $3)",
            job_id,
            status,
            pr_url,
        )
    await _reset(db)

    for _ in range(2):  # second pass must be a no-op
        await db.execute(_sql_for(73))
        for job_id, status, _, expected in rows:
            row = await db.fetchrow(
                "SELECT status, delivery_state, delivery_state_at FROM jobs WHERE job_id = $1", job_id
            )
            assert (row["status"], row["delivery_state"], row["delivery_state_at"]) == (status, expected, None), job_id


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_73_never_overwrites_a_state_merge_sync_already_recorded(
    db_with_migrations: DatabasePool,
) -> None:
    db = db_with_migrations
    await db.execute(
        "INSERT INTO jobs (job_id, repo, task, status, pr_url, delivery_state) "
        "VALUES ('j', 'acme/widgets', 't', 'failed', $1, 'merged')",
        PR,
    )
    await db.execute(_sql_for(73))
    assert await db.fetchval("SELECT delivery_state FROM jobs WHERE job_id = 'j'") == "merged"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_73_does_not_touch_jobs_updated_at_and_rearms_the_trigger(
    db_with_migrations: DatabasePool,
) -> None:
    db = db_with_migrations
    await db.execute("INSERT INTO jobs (job_id, repo, task, status, pr_url) VALUES ('j', 'o/r', 't', 'failed', $1)", PR)
    await db.execute("ALTER TABLE jobs DISABLE TRIGGER trg_jobs_updated_at")
    await db.execute("UPDATE jobs SET updated_at = '2026-08-01T00:00:00Z', delivery_state = 'none' WHERE job_id = 'j'")
    await db.execute("ALTER TABLE jobs ENABLE TRIGGER trg_jobs_updated_at")
    before = await db.fetchval("SELECT updated_at FROM jobs WHERE job_id = 'j'")

    await db.execute(_sql_for(73))

    assert await db.fetchval("SELECT delivery_state FROM jobs WHERE job_id = 'j'") == "pr_open"
    assert await db.fetchval("SELECT updated_at FROM jobs WHERE job_id = 'j'") == before
    assert await db.fetchval("SELECT tgenabled::text FROM pg_trigger WHERE tgname = 'trg_jobs_updated_at'") == "O"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_delivery_state_is_a_closed_vocabulary(db_with_migrations: DatabasePool) -> None:
    from embry0.services.merge_sync.facts import DeliveryState

    db = db_with_migrations
    definition = await db.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'jobs_delivery_state_check'"
    )
    for state in DeliveryState:
        assert f"'{state.value}'" in definition
    with pytest.raises(Exception, match="jobs_delivery_state_check"):
        await db.execute("INSERT INTO jobs (job_id, repo, task, delivery_state) VALUES ('j', 'o/r', 't', 'shipped')")
