"""Migration 69 — the healing_cases lifecycle ledger (#278)."""

import re

import asyncpg
import pytest

from embry0.services.healing.lifecycle import ResolutionPath, Stage, Verdict
from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == 69]
    assert len(match) == 1
    return match[0]


def _check_values(column: str) -> set[str]:
    """The CHECK list the schema ends up with, not the one migration 69 wrote.

    A later migration may replace the predicate (84 added ``abandoned`` to
    resolution_path), so reading 69 alone would fail the moment the vocabulary
    grew — and, worse, would still pass while the live constraint and the enum
    disagreed if a value were ever removed. Scanning every migration in order
    and keeping the LAST definition is what the database actually has.
    """
    found = None
    for _, _, sql in MIGRATIONS:
        for match in re.finditer(rf"CHECK \({column} IN \(([^)]*)\)", sql):
            found = match
    assert found, f"no CHECK list for {column}"
    return set(re.findall(r"'([a-z0-9_]+)'", found.group(1)))


def test_migrations_stay_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))


@pytest.mark.parametrize(
    ("column", "vocabulary"),
    [("stage", Stage), ("resolution_path", ResolutionPath), ("healed_verdict", Verdict)],
)
def test_check_constraints_match_the_lifecycle_vocabulary(column: str, vocabulary: type) -> None:
    assert _check_values(column) == {member.value for member in vocabulary}


def test_origin_is_validated_in_code_not_by_a_check() -> None:
    # An open list of detection sources; see the migration comment.
    assert "CHECK (origin" not in _sql()


def test_ddl_is_idempotent() -> None:
    sql = _sql()
    assert "CREATE TABLE healing" not in sql
    assert re.findall(r"CREATE (?:UNIQUE )?INDEX (?!IF NOT EXISTS)", sql) == []


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_replays_cleanly(db_with_migrations: DatabasePool) -> None:
    await db_with_migrations.execute(_sql())


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_links_are_real_foreign_keys(db_with_migrations: DatabasePool) -> None:
    rows = await db_with_migrations.fetch(
        """
        SELECT conrelid::regclass::text AS child, confrelid::regclass::text AS parent
        FROM pg_constraint
        WHERE contype = 'f' AND conrelid::regclass::text LIKE 'healing_case%'
        """
    )
    assert {(r["child"], r["parent"]) for r in rows} == {
        ("healing_cases", "watcher_runs"),
        ("healing_cases", "issues"),
        ("healing_cases", "healing_cases"),
        ("healing_case_prs", "healing_cases"),
        ("healing_case_prs", "jobs"),
        ("healing_case_jobs", "healing_cases"),
        ("healing_case_jobs", "jobs"),
        ("healing_case_events", "healing_cases"),
    }
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db_with_migrations.execute(
            "INSERT INTO healing_cases (id, repo, origin, issue_id) VALUES ('hc-x', 'a/b', 'human', 'iss-missing')"
        )


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_tracker_needs_both_kind_and_key(db_with_migrations: DatabasePool) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await db_with_migrations.execute(
            "INSERT INTO healing_cases (id, repo, origin, tracker) VALUES ('hc-x', 'a/b', 'human', 'linear')"
        )


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_ticket_has_one_open_case_and_any_number_of_closed_ones(db_with_migrations: DatabasePool) -> None:
    insert = "INSERT INTO healing_cases (id, repo, origin, tracker, tracker_key, stage) VALUES ($1, 'a/b', 'human', 'linear', 'ENG-1', $2)"
    await db_with_migrations.execute(insert, "hc-1", "closed")
    await db_with_migrations.execute(insert, "hc-2", "closed")
    await db_with_migrations.execute(insert, "hc-3", "tracked")
    with pytest.raises(asyncpg.UniqueViolationError):
        await db_with_migrations.execute(insert, "hc-4", "accepted")


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_an_event_key_is_unique_per_case(db_with_migrations: DatabasePool) -> None:
    await db_with_migrations.execute("INSERT INTO healing_cases (id, repo, origin) VALUES ('hc-1', 'a/b', 'human')")
    insert = (
        "INSERT INTO healing_case_events (case_id, event, event_key, to_stage) VALUES ('hc-1', 'merged', $1, 'merged')"
    )
    await db_with_migrations.execute(insert, "delivery-1")
    await db_with_migrations.execute(insert, None)
    await db_with_migrations.execute(insert, None)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db_with_migrations.execute(insert, "delivery-1")
