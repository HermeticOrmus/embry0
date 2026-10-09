"""Migration 60 — generic tracker reference columns (#165)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS
from embry0.storage.repositories.issues import IssuesRepository

_COLUMNS = ("tracker", "tracker_key", "tracker_url")


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1
    return match[0][2]


def test_migration_60_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 60 in versions
    assert versions == list(range(1, len(versions) + 1))


def test_tracker_key_is_text_not_integer() -> None:
    """#161 must fold Linear's "ENG-123" into this column alongside integers."""
    sql = _sql_for(60)
    assert "tracker_key  TEXT" in sql or "tracker_key TEXT" in sql
    assert "tracker_key INTEGER" not in sql


def test_unique_index_is_partial() -> None:
    """Existing github_*/linear_* rows have no tracker ref and must be exempt."""
    sql = _sql_for(60)
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_issues_tracker_ref" in sql
    assert "WHERE tracker IS NOT NULL AND tracker_key IS NOT NULL" in sql


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_tracker_ref_round_trips(db_with_migrations: DatabasePool) -> None:
    repo = IssuesRepository(db_with_migrations)
    issue_id = await repo.create(title="t", repo="acme/products/fbr")
    await repo.update(issue_id, tracker="gitlab", tracker_key="11", tracker_url="https://x/-/issues/11")

    found = await repo.get_by_tracker_ref("gitlab", "acme/products/fbr", "11")
    assert found is not None and found["id"] == issue_id
    assert await repo.get_by_tracker_ref("gitlab", "other/proj", "11") is None
    assert await repo.get_by_tracker_ref("github", "acme/products/fbr", "11") is None


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_same_iid_in_two_projects_does_not_collide(db_with_migrations: DatabasePool) -> None:
    """A GitLab iid is only unique within its project, hence the repo scope."""
    repo = IssuesRepository(db_with_migrations)
    a = await repo.create(title="a", repo="group/one")
    b = await repo.create(title="b", repo="group/two")
    await repo.update(a, tracker="gitlab", tracker_key="1")
    await repo.update(b, tracker="gitlab", tracker_key="1")

    assert (await repo.get_by_tracker_ref("gitlab", "group/one", "1"))["id"] == a
    assert (await repo.get_by_tracker_ref("gitlab", "group/two", "1"))["id"] == b


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_existing_rows_without_a_tracker_are_unaffected(db_with_migrations: DatabasePool) -> None:
    """Many rows will have NULL tracker; the partial index must allow that."""
    repo = IssuesRepository(db_with_migrations)
    first = await repo.create(title="a", repo="o/r")
    second = await repo.create(title="b", repo="o/r")
    assert first != second  # both NULL-tracker rows coexist
