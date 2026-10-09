"""Migration 65 — a numbered no-op (#527).

Version 65 once rewrote stored URLs after one deployment's repo rename
(#277). A rename is that operator's configuration, so the rewrite is gone;
the entry stays because the runner keys on the integer and the list must
stay gapless.
"""

import re

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS, run_migrations


def _entry(version: int) -> tuple[int, str, str]:
    match = [m for m in MIGRATIONS if m[0] == version]
    assert len(match) == 1, f"migration {version} must appear exactly once, found {len(match)}"
    return match[0]


def test_migration_65_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 65 in versions
    assert versions == list(range(1, len(versions) + 1))


def test_migration_65_is_a_no_op() -> None:
    _, _, sql = _entry(65)
    statements = [s.strip() for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]
    assert statements == ["SELECT 1"], f"migration 65 must stay a no-op, it runs {statements!r}"


def test_migration_65_names_no_repository() -> None:
    """The product carries no operator's repo, in the SQL or in the description
    every new ledger row records."""
    _, description, sql = _entry(65)
    for text in (description, sql):
        assert "://" not in text, f"migration 65 names a URL: {text!r}"
        assert not re.search(r"[\w.-]+/[\w.-]+", text), f"migration 65 names an owner/repo: {text!r}"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_65_touches_no_table(db_with_migrations: DatabasePool) -> None:
    """Reading or writing a table locks it until the transaction ends, so a
    migration that holds no lock on any table touched none."""
    db = db_with_migrations
    assert db.pool is not None
    _, _, sql = _entry(65)
    async with db.pool.acquire() as conn, conn.transaction():
        await conn.execute(sql)
        locked = await conn.fetch(
            """
            SELECT c.relname, l.mode
              FROM pg_locks l
              JOIN pg_class c ON c.oid = l.relation
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE l.pid = pg_backend_pid() AND n.nspname = 'public'
             ORDER BY c.relname, l.mode
            """
        )
    assert [(r["relname"], r["mode"]) for r in locked] == [], "migration 65 touched a table"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_database_that_applied_the_old_65_is_left_alone(db_with_migrations: DatabasePool) -> None:
    """A database migrated before #527 keeps its ledger row and its data: the
    runner skips a recorded version by membership, so 65 never runs there
    again."""
    db = db_with_migrations
    await db.execute(
        "UPDATE embry0_migrations SET description = 'the pre-#527 rewrite', "
        "applied_at = '2026-09-20T04:33:11Z' WHERE version = 65"
    )
    url = "https://github.com/acme/widgets/pull/1"
    await db.execute("INSERT INTO jobs (job_id, repo, task, pr_url) VALUES ('j-1', 'acme/widgets', 't', $1)", url)
    await db.execute("ALTER TABLE jobs DISABLE TRIGGER trg_jobs_updated_at")
    await db.execute("UPDATE jobs SET updated_at = '2026-08-01T00:00:00Z' WHERE job_id = 'j-1'")
    await db.execute("ALTER TABLE jobs ENABLE TRIGGER trg_jobs_updated_at")
    before = await db.fetchrow("SELECT pr_url, updated_at FROM jobs WHERE job_id = 'j-1'")

    await run_migrations(db)

    ledger = await db.fetchrow(
        "SELECT description, applied_at = '2026-09-20T04:33:11Z' AS same_time FROM embry0_migrations WHERE version = 65"
    )
    assert ledger is not None
    assert (ledger["description"], ledger["same_time"]) == ("the pre-#527 rewrite", True), (
        "an applied migration 65 was re-recorded"
    )
    after = await db.fetchrow("SELECT pr_url, updated_at FROM jobs WHERE job_id = 'j-1'")
    assert dict(after) == dict(before), "re-running the migrations changed a stored job"
