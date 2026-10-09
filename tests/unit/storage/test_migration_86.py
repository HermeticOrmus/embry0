"""Migration 86 — credential registry tables (spec 2026-09-29 §4)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1, f"migration {version} must exist exactly once"
    return match[0][2]


def test_migration_86_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 86 in versions
    assert versions == list(range(1, len(versions) + 1))


def test_migration_86_declares_the_three_tables() -> None:
    sql = _sql_for(86)
    for table in ("credentials", "credential_bindings", "credential_uses"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "credential_bindings_single" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "ON DELETE SET NULL" in sql


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_pool_allows_many_global_llm_bindings_but_one_repo_forge(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    for i in (1, 2):
        await db.execute(
            "INSERT INTO credentials (id, kind, provider, host, label, secret_ciphertext, masked_preview, created_by)"
            " VALUES ($1, 'llm_oauth', 'anthropic', 'api.anthropic.com', $2, 'x', '****', 'test')",
            f"cred-{i:012x}",
            f"claude-{i}",
        )
        await db.execute(
            "INSERT INTO credential_bindings (scope_kind, scope_value, role, provider, credential_id, created_by)"
            " VALUES ('global', '', 'llm', 'anthropic', $1, 'test')",
            f"cred-{i:012x}",
        )
    for i in (3, 4):
        await db.execute(
            "INSERT INTO credentials (id, kind, provider, host, label, secret_ciphertext, masked_preview, created_by)"
            " VALUES ($1, 'forge_token', 'github', 'github.com', $2, 'x', '****', 'test')",
            f"cred-{i:012x}",
            f"gh-{i}",
        )
    await db.execute(
        "INSERT INTO credential_bindings (scope_kind, scope_value, role, provider, credential_id, created_by)"
        " VALUES ('repo', 'acme/widgets', 'forge', 'github', $1, 'test')",
        "cred-000000000003",
    )
    import asyncpg

    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "INSERT INTO credential_bindings (scope_kind, scope_value, role, provider, credential_id, created_by)"
            " VALUES ('repo', 'acme/widgets', 'forge', 'github', $1, 'test')",
            "cred-000000000004",
        )
