"""Migration 58 — Linear credentials on integration_config (#157)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS

_COLUMNS = ("linear_api_key", "linear_webhook_secret", "linear_repo_map")


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1
    return match[0][2]


def test_migration_58_present_and_ordered() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 58 in versions
    assert versions == sorted(versions)
    assert versions == list(range(1, len(versions) + 1))


def test_migration_58_is_idempotent_ddl() -> None:
    sql = _sql_for(58)
    assert sql.count("ADD COLUMN IF NOT EXISTS") == 3
    for column in _COLUMNS:
        assert column in sql


def test_migration_58_documents_every_column() -> None:
    """House style: each new column carries a COMMENT ON COLUMN."""
    sql = _sql_for(58)
    for column in _COLUMNS:
        assert f"COMMENT ON COLUMN integration_config.{column}" in sql


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_58_creates_the_columns(db_with_migrations: DatabasePool) -> None:
    rows = await db_with_migrations.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'integration_config' AND column_name = ANY($1::text[])",
        list(_COLUMNS),
    )
    assert {r["column_name"] for r in rows} == set(_COLUMNS)
