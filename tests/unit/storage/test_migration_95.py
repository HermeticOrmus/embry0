"""Migration 95 — repo_records / repo_record_revisions."""

from __future__ import annotations

import re

import asyncpg
import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS
from embry0.storage.repositories.repo_records import RECORD_KINDS, SLUG_PATTERN


def _sql_for(version: int) -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == version]
    assert len(match) == 1, f"migration {version} must exist exactly once"
    return match[0]


def test_migration_95_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 95 in versions, "migration 95 (repo records) is missing"
    assert versions == list(range(1, len(versions) + 1))


def test_migration_95_is_the_last_entry() -> None:
    """The newest migration owns this assertion; it passes to the next one
    (it came from tests/unit/storage/test_migration_94.py)."""
    assert MIGRATIONS[-1][0] == 95, "migration 95 must create the repo records tables"


def test_kind_check_matches_the_python_list() -> None:
    """The CHECK constraint and RECORD_KINDS are one list written twice."""
    match = re.search(r"CHECK \(kind IN \(([^)]*)\)\)", _sql_for(95))
    assert match, "migration 95 lost its kind CHECK constraint"
    in_sql = tuple(k.strip().strip("'") for k in match.group(1).split(","))
    assert in_sql == RECORD_KINDS, f"kinds differ: SQL {in_sql} vs Python {RECORD_KINDS}"


def test_slug_check_matches_the_python_pattern() -> None:
    match = re.search(r"CHECK \(slug ~ '([^']*)'\)", _sql_for(95))
    assert match, "migration 95 lost its slug CHECK constraint"
    assert match.group(1) == SLUG_PATTERN.pattern.replace("\\Z", "$"), (
        f"slug rule differs: SQL {match.group(1)!r} vs Python {SLUG_PATTERN.pattern!r}"
    )


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_tables_exist_and_the_migration_is_idempotent(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    await db.execute(_sql_for(95))  # a second run must be a no-op
    for table in ("repo_records", "repo_record_revisions"):
        assert await db.fetchval("SELECT to_regclass($1)", table) is not None, f"{table} missing"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_the_database_refuses_an_unknown_kind_and_a_bad_slug(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    await db.execute("INSERT INTO repos (repo, forge) VALUES ('acme/widgets', 'github')")
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO repo_records (repo, slug, kind, title) VALUES ('acme/widgets', 'a', 'diary', 't')"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO repo_records (repo, slug, kind, title) VALUES ('acme/widgets', 'Has Space', 'note', 't')"
        )


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_repo_with_records_cannot_be_deleted_but_can_be_renamed(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    await db.execute("INSERT INTO repos (repo, forge) VALUES ('acme/widgets', 'github')")
    await db.execute("INSERT INTO repo_records (repo, slug, kind, title) VALUES ('acme/widgets', 'a', 'note', 't')")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.execute("DELETE FROM repos WHERE repo = 'acme/widgets'")
    await db.execute("UPDATE repos SET repo = 'acme/gadgets' WHERE repo = 'acme/widgets'")
    assert await db.fetchval("SELECT repo FROM repo_records WHERE slug = 'a'") == "acme/gadgets"
