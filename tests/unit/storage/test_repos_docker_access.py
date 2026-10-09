"""repos.docker_access — the admin's per-repo grant of the shared Docker daemon (#522)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.repositories.repos import ReposRepository


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_registered_repo_starts_without_docker_access(db_with_migrations: DatabasePool) -> None:
    repos = ReposRepository(db_with_migrations)
    row = await repos.register("acme/widgets")
    assert row["docker_access"] is False, row
    assert await repos.docker_access("acme/widgets") is False


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_grant_and_revoke_round_trip(db_with_migrations: DatabasePool) -> None:
    repos = ReposRepository(db_with_migrations)
    await repos.register("acme/widgets")
    await repos.register("acme/gadgets")

    row = await repos.set_docker_access("acme/widgets", True)
    assert row is not None and row["docker_access"] is True, row
    assert await repos.docker_access("acme/widgets") is True
    assert await repos.docker_access("acme/gadgets") is False, "a grant must not leak to another repo"

    await repos.set_docker_access("acme/widgets", False)
    assert await repos.docker_access("acme/widgets") is False


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_re_registering_keeps_the_grant(db_with_migrations: DatabasePool) -> None:
    """register() is idempotent; it must neither revoke nor (on a new row) grant."""
    repos = ReposRepository(db_with_migrations)
    await repos.register("acme/widgets")
    await repos.set_docker_access("acme/widgets", True)
    row = await repos.register("acme/widgets", notes="again")
    assert row["docker_access"] is True, row


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_unregistered_repo_has_no_access_and_cannot_be_granted(db_with_migrations: DatabasePool) -> None:
    repos = ReposRepository(db_with_migrations)
    assert await repos.docker_access("acme/ghost") is False
    assert await repos.set_docker_access("acme/ghost", True) is None
    assert await db_with_migrations.fetchval("SELECT count(*) FROM repos") == 0


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_deleting_a_repo_drops_its_grant(db_with_migrations: DatabasePool) -> None:
    """A grant belongs to the registry row: a re-registered repo starts over."""
    repos = ReposRepository(db_with_migrations)
    await repos.register("acme/widgets")
    await repos.set_docker_access("acme/widgets", True)
    await repos.delete("acme/widgets")
    await repos.register("acme/widgets")
    assert await repos.docker_access("acme/widgets") is False
