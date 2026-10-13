"""Migration 88 — api_keys.allowed_repos, the per-key repository allowlist."""

import asyncpg
import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1, f"migration {version} must exist exactly once"
    return match[0][2]


def test_the_column_is_nullable_so_existing_keys_stay_unrestricted() -> None:
    sql = _sql_for(88)
    assert "ADD COLUMN IF NOT EXISTS allowed_repos TEXT[];" in sql, "allowed_repos must be a nullable TEXT[]"


async def _insert(db: DatabasePool, key_id: str, allowed: list[str] | None) -> None:
    await db.execute(
        "INSERT INTO api_keys (id, name, key_hash, prefix, scope, allowed_repos) VALUES ($1, $1, $1, 'emb_x', 'operate', $2)",
        key_id,
        allowed,
    )


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_existing_keys_read_back_unrestricted(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    await db.execute(
        "INSERT INTO api_keys (id, name, key_hash, prefix, scope) VALUES ('k1', 'k1', 'h1', 'emb_x', 'read')"
    )
    assert await db.fetchval("SELECT allowed_repos FROM api_keys WHERE id = 'k1'") is None, (
        "a key minted without an allowlist must read back as unrestricted (NULL)"
    )


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_an_empty_allowlist_is_refused(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    await _insert(db, "k-listed", ["acme/widgets"])
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(db, "k-empty", [])


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_rerunning_the_migration_is_harmless(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    await _insert(db, "k-listed", ["acme/widgets"])
    await db.execute(_sql_for(88))
    assert await db.fetchval("SELECT allowed_repos FROM api_keys WHERE id = 'k-listed'") == ["acme/widgets"]
