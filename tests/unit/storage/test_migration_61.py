"""Migration 61 — fold github_*/linear_* refs onto the generic tracker columns (#161)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS
from embry0.storage.repositories.issues import IssuesRepository

_DROPPED = ("github_number", "github_url", "linear_identifier", "linear_issue_id")


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1
    return match[0][2]


def test_migration_61_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 61 in versions
    assert versions == list(range(1, len(versions) + 1))


def test_backfills_before_dropping() -> None:
    """The UPDATEs must run while the source columns still exist."""
    sql = _sql_for(61)
    for col in _DROPPED:
        assert f"DROP COLUMN IF EXISTS {col}" in sql
    github_backfill = sql.index("tracker_key = github_number::text")
    linear_backfill = sql.index("tracker_key = linear_identifier")
    first_drop = sql.index("DROP COLUMN")
    assert github_backfill < first_drop and linear_backfill < first_drop


def test_backfill_never_overwrites_an_existing_tracker_ref() -> None:
    """GitLab rows (and re-runs) already carry tracker — the guard keeps them."""
    sql = _sql_for(61)
    assert sql.count("AND tracker IS NULL") == 2


def test_sync_feature_flags_survive() -> None:
    """github_sync_enabled / github_synced_at gate the outbound push feature —
    they are not tracker refs and must not be dropped."""
    sql = _sql_for(61)
    assert "github_sync_enabled" not in sql
    assert "github_synced_at" not in sql


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_dropped_columns_are_gone(db_with_migrations: DatabasePool) -> None:
    rows = await db_with_migrations.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'issues'"
    )
    columns = {r["column_name"] for r in rows}
    for col in _DROPPED:
        assert col not in columns
    assert {"tracker", "tracker_key", "tracker_url", "tracker_native_id"} <= columns


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_backfill_folds_legacy_rows(db_with_migrations: DatabasePool) -> None:
    """Re-create the legacy columns, seed old-shaped rows, re-run 61's SQL."""
    repo = IssuesRepository(db_with_migrations)
    gh = await repo.create(title="gh", repo="o/r")
    lin = await repo.create(title="lin", repo="o/r2")
    for col, sqltype in (
        ("github_number", "INTEGER"),
        ("github_url", "TEXT"),
        ("linear_identifier", "TEXT"),
        ("linear_issue_id", "TEXT"),
    ):
        await db_with_migrations.execute(f"ALTER TABLE issues ADD COLUMN IF NOT EXISTS {col} {sqltype}")
    await db_with_migrations.execute(
        "UPDATE issues SET github_number = 42, github_url = 'https://gh/42' WHERE id = $1", gh
    )
    await db_with_migrations.execute(
        "UPDATE issues SET linear_identifier = 'ENG-9', linear_issue_id = 'uuid-9' WHERE id = $1", lin
    )

    for statement in _sql_for(61).split(";"):
        if statement.strip():
            await db_with_migrations.execute(statement)

    folded_gh = await repo.get_by_tracker_ref("github", "o/r", "42")
    assert folded_gh is not None and folded_gh["id"] == gh
    assert folded_gh["tracker_url"] == "https://gh/42"

    folded_lin = await repo.get_by_tracker_ref("linear", None, "ENG-9")
    assert folded_lin is not None and folded_lin["id"] == lin
    assert folded_lin["tracker_native_id"] == "uuid-9"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_repo_less_lookup_and_enrichment(db_with_migrations: DatabasePool) -> None:
    """get_by_tracker_ref(repo=None) serves Linear's pre-routing dedupe, and
    every hit carries the same enrichment as get()."""
    repo = IssuesRepository(db_with_migrations)
    issue_id = await repo.create(title="t", repo="o/r")
    await repo.update(issue_id, tracker="linear", tracker_key="ENG-1", tracker_native_id="uuid-1")

    found = await repo.get_by_tracker_ref("linear", None, "ENG-1")
    assert found is not None and found["id"] == issue_id
    assert found["children_count"] == 0 and found["jobs_count"] == 0
