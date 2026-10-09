"""Migration 64 — repo_preferences.base_branch (Spec 1A §3.7)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS
from embry0.storage.repositories.repo_preferences import RepoPreferencesRepository


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1
    return match[0][2]


def test_migration_64_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 64 in versions
    assert versions == list(range(1, len(versions) + 1))


def test_migration_64_adds_the_column_idempotently() -> None:
    sql = _sql_for(64)
    assert "ADD COLUMN IF NOT EXISTS base_branch" in sql
    assert "COMMENT ON COLUMN repo_preferences.base_branch" in sql


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_base_branch_round_trips(db_with_migrations: DatabasePool) -> None:
    await db_with_migrations.execute("DELETE FROM repo_preferences")
    repo = RepoPreferencesRepository(db_with_migrations)

    row = await repo.upsert(repo="acme/group/backend", base_branch="dev")
    assert row["base_branch"] == "dev"

    # NULL is the "ask the forge" signal and must survive a round trip.
    cleared = await repo.upsert(repo="acme/group/backend", base_branch=None)
    assert cleared["base_branch"] is None

    assert (await repo.get("acme/group/backend"))["base_branch"] is None
