"""Migration 59 — repos.forge discriminator (#162)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS
from embry0.storage.repositories.repos import DEFAULT_FORGE, SUPPORTED_FORGES, ReposRepository


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1
    return match[0][2]


def test_migration_59_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 59 in versions
    assert versions == list(range(1, len(versions) + 1))


def test_migration_59_backfills_existing_rows_as_github() -> None:
    """Before #162 GitHub was the only possibility, so the default is the
    correct historical answer for every pre-existing row."""
    sql = _sql_for(59)
    assert "DEFAULT 'github'" in sql
    assert "NOT NULL" in sql
    assert "ADD COLUMN IF NOT EXISTS forge" in sql
    assert "COMMENT ON COLUMN repos.forge" in sql


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_forge_defaults_to_github(db_with_migrations: DatabasePool) -> None:
    repo = ReposRepository(db_with_migrations)
    row = await repo.register("acme/widgets")
    assert row["forge"] == DEFAULT_FORGE == "github"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_registers_a_nested_subgroup_gitlab_repo(db_with_migrations: DatabasePool) -> None:
    repo = ReposRepository(db_with_migrations)
    row = await repo.register("acme/products/fbr", forge="gitlab")
    assert row["repo"] == "acme/products/fbr"
    assert row["forge"] == "gitlab"
    assert await repo.forge_for("acme/products/fbr") == "gitlab"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_unknown_forge_is_rejected_at_the_boundary(db_with_migrations: DatabasePool) -> None:
    """A bad value would register a repo nothing can clone; fail loudly here
    rather than at clone time."""
    repo = ReposRepository(db_with_migrations)
    with pytest.raises(ValueError, match="unsupported forge"):
        await repo.register("acme/widgets", forge="bitbucket")
    assert set(SUPPORTED_FORGES) == {"github", "gitlab"}


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_forge_for_unregistered_repo_defaults_to_github(db_with_migrations: DatabasePool) -> None:
    """Jobs can be submitted for repos that were never registered."""
    repo = ReposRepository(db_with_migrations)
    assert await repo.forge_for("never/registered") == "github"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_forge_survives_idempotent_reregistration(db_with_migrations: DatabasePool) -> None:
    """register() is ON CONFLICT DO NOTHING — a second call must not silently
    downgrade a gitlab repo back to the github default."""
    repo = ReposRepository(db_with_migrations)
    await repo.register("acme/products/fbt", forge="gitlab")
    again = await repo.register("acme/products/fbt")
    assert again["forge"] == "gitlab"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_register_inherits_the_configured_default_not_the_module_constant(
    db_with_migrations: DatabasePool,
) -> None:
    repo = ReposRepository(db_with_migrations, default_forge="gitlab")
    row = await repo.register("acme/group/backend")
    assert row["forge"] == "gitlab" != DEFAULT_FORGE


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_register_without_a_forge_refuses_when_the_deployment_has_no_default(
    db_with_migrations: DatabasePool,
) -> None:
    """Spec 1A §3.8: a guessed row silences the refusal forever after."""
    repo = ReposRepository(db_with_migrations, default_forge="")
    with pytest.raises(ValueError, match="needs an explicit forge"):
        await repo.register("acme/group/backend")
    assert await repo.get("acme/group/backend") is None
