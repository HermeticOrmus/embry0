"""Migration 57 — bump builtin agent models to the Claude 5 family."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS
from embry0.storage.repositories.agent_definitions import BUILTIN_SEED

# Types migration 2 seeded and that migration 57 is responsible for bumping.
# The rest of BUILTIN_SEED arrives via the startup seeders, already current.
_MIGRATION_SEEDED = ("triage", "developer", "review")


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1
    return match[0][2]


def test_migration_57_present_and_ordered() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 57 in versions
    assert versions == sorted(versions)
    assert versions[-1] >= 57


def test_migration_57_is_guarded_on_prior_seed_values() -> None:
    """An operator who picked their own model must never be clobbered."""
    sql = _sql_for(57)
    assert "claude-sonnet-4-6" in sql
    assert "claude-opus-4-6" in sql  # installs predating migration 14
    assert "claude-opus-4-7" in sql  # what migration 14 left behind
    assert sql.count("is_builtin = true") == 2


def test_migration_57_targets_match_builtin_seed() -> None:
    """The values written must be exactly what BUILTIN_SEED declares today."""
    sql = _sql_for(57)
    for agent_type in _MIGRATION_SEEDED:
        assert f"'{BUILTIN_SEED[agent_type]['model']}'" in sql


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_57_reconciles_rows_with_builtin_seed(
    db_with_migrations: DatabasePool,
) -> None:
    """After a full migration run, no migration-seeded row is left stale.

    This is the regression that made test_agent_definitions fail: migrations
    run before the insert-if-missing startup seeder, so nothing reconciled the
    rows and a fresh install dispatched on superseded models.
    """
    rows = await db_with_migrations.fetch(
        "SELECT type, model FROM agent_definitions WHERE type = ANY($1::text[])",
        list(_MIGRATION_SEEDED),
    )
    actual = {r["type"]: r["model"] for r in rows}
    assert actual == {t: BUILTIN_SEED[t]["model"] for t in _MIGRATION_SEEDED}


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_57_leaves_customised_models_alone(
    db_with_migrations: DatabasePool,
) -> None:
    """Re-running the guarded UPDATE must not touch an operator's choice."""
    await db_with_migrations.execute("UPDATE agent_definitions SET model = 'my-own-model' WHERE type = 'triage'")
    await db_with_migrations.execute(_sql_for(57))
    model = await db_with_migrations.fetchval("SELECT model FROM agent_definitions WHERE type = 'triage'")
    assert model == "my-own-model"
