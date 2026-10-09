"""job_verifications (#298/#299) + migration 72, against real postgres."""

import re

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS
from embry0.storage.repositories.job_verifications import JobVerificationsRepository


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == 72]
    assert len(match) == 1
    return match[0]


def test_ddl_is_idempotent() -> None:
    sql = _sql()
    assert re.findall(r"CREATE TABLE (?!IF NOT EXISTS)", sql) == []
    assert re.findall(r"CREATE (?:UNIQUE )?INDEX (?!IF NOT EXISTS)", sql) == []
    assert re.findall(r"ADD COLUMN (?!IF NOT EXISTS)", sql) == []


def _result(verdict: str = "passed", head: str = "h" * 40, human: bool = False) -> dict:
    return {
        "schema_version": 1,
        "mode": "on",
        "tier": {"tier": "backend_only"},
        "gate": {"verdict": verdict, "base_sha": "b" * 40, "head_sha": head, "runs": []},
        "browser_qa": {"planned": "skipped", "reason_code": "backend_only"},
        "blocking": verdict == "new_failures",
        "human_merge_only": human,
        "human_merge_reasons": [],
    }


@pytest.fixture
async def repo(db_with_migrations: DatabasePool) -> JobVerificationsRepository:
    for job_id in ("job-1", "job-2"):
        await db_with_migrations.execute("INSERT INTO jobs (job_id, task, repo) VALUES ($1, 't', 'acme/app')", job_id)
    return JobVerificationsRepository(db_with_migrations)


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_replays_cleanly(db_with_migrations: DatabasePool) -> None:
    await db_with_migrations.execute(_sql())


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_upsert_then_get_exposes_the_scalars_and_the_report(repo: JobVerificationsRepository) -> None:
    await repo.upsert(job_id="job-1", repo="acme/app", result=_result("new_failures"))
    row = await repo.get("job-1")
    assert row is not None
    assert (row["tier"], row["gate_verdict"], row["blocking"], row["browser_qa"]) == (
        "backend_only",
        "new_failures",
        True,
        "skipped",
    )
    assert row["report"]["gate"]["verdict"] == "new_failures"
    assert row["published_at"] is None


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_second_result_replaces_the_first_and_is_unpublished_again(repo: JobVerificationsRepository) -> None:
    await repo.upsert(job_id="job-1", repo="acme/app", result=_result("new_failures"))
    await repo.mark_published("job-1")
    assert (await repo.get("job-1"))["published_at"] is not None  # type: ignore[index]
    await repo.upsert(job_id="job-1", repo="acme/app", result=_result("passed"))
    row = await repo.get("job-1")
    assert row is not None
    assert (row["gate_verdict"], row["blocking"], row["published_at"]) == ("passed", False, None)


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_lookup_by_head_never_answers_for_another_commit(repo: JobVerificationsRepository) -> None:
    await repo.upsert(job_id="job-1", repo="acme/app", result=_result(head="1" * 40))
    await repo.upsert(job_id="job-2", repo="acme/app", result=_result(head="2" * 40))
    found = await repo.latest_for_head("acme/app", "2" * 40)
    assert found is not None and found["job_id"] == "job-2"
    assert await repo.latest_for_head("acme/app", "3" * 40) is None
    assert await repo.latest_for_head("other/app", "2" * 40) is None
    assert await repo.latest_for_head("acme/app", "") is None


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_rows_die_with_their_job(repo: JobVerificationsRepository, db_with_migrations: DatabasePool) -> None:
    await repo.upsert(job_id="job-1", repo="acme/app", result=_result())
    await db_with_migrations.execute("DELETE FROM jobs WHERE job_id = 'job-1'")
    assert await repo.get("job-1") is None
