"""jobs.delivery_state storage: the locked write path and its reads (#280)."""

from datetime import UTC, datetime, timedelta

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.repositories.job_delivery import DeliveryWrite, JobDeliveryRepository
from embry0.storage.repositories.jobs import VALID_JOB_TRANSITIONS, JobsRepository
from embry0.storage.schemas import JobStatus

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

PR = "https://github.com/acme/widgets/pull/1"
WHEN = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)


async def _job(db: DatabasePool, status: str = "completed", **create) -> str:
    job_id = await JobsRepository(db).create(repo="acme/widgets", task="t", **create)
    await db.execute("UPDATE jobs SET status = $2 WHERE job_id = $1", job_id, status)
    return job_id


async def test_a_new_job_has_delivered_nothing(db_with_migrations):
    job_id = await _job(db_with_migrations)
    assert await db_with_migrations.fetchval("SELECT delivery_state FROM jobs WHERE job_id = $1", job_id) == "none"


async def test_storing_a_pr_url_lifts_none_to_pr_open_and_nothing_else(db_with_migrations):
    db = db_with_migrations
    jobs = JobsRepository(db)
    fresh = await _job(db)
    merged = await _job(db)
    await db.execute("UPDATE jobs SET delivery_state = 'merged' WHERE job_id = $1", merged)

    await jobs.update(fresh, pr_url=PR)
    await jobs.update(merged, pr_url=PR)
    await jobs.update(fresh, error_message="unrelated")

    assert await db.fetchval("SELECT delivery_state FROM jobs WHERE job_id = $1", fresh) == "pr_open"
    assert await db.fetchval("SELECT delivery_state FROM jobs WHERE job_id = $1", merged) == "merged"


async def test_a_closed_pr_can_be_reopened_or_merged_but_a_merge_is_final():
    assert VALID_JOB_TRANSITIONS[JobStatus.PR_CLOSED] == {JobStatus.PR_MERGED, JobStatus.COMPLETED}
    assert VALID_JOB_TRANSITIONS[JobStatus.PR_MERGED] == set()
    assert VALID_JOB_TRANSITIONS[JobStatus.COMPLETED] == {JobStatus.PR_MERGED, JobStatus.PR_CLOSED}
    for terminal in (JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.PARTIAL, JobStatus.EXPIRED):
        assert VALID_JOB_TRANSITIONS[terminal] == set()  # delivery never moves these


async def test_apply_decides_against_the_locked_row_and_only_fills_an_empty_pr_url(db_with_migrations):
    db = db_with_migrations
    repo = JobDeliveryRepository(db)
    job_id = await _job(db)
    seen: list[str] = []

    def decide(row):
        seen.append(row["delivery_state"])
        return DeliveryWrite(delivery_state="merged", delivery_state_at=WHEN, pr_url=PR, status="pr_merged")

    before, _ = await repo.apply(job_id, decide)
    await repo.apply(job_id, lambda row: DeliveryWrite(pr_url="https://github.com/acme/widgets/pull/2"))

    row = await db.fetchrow(
        "SELECT status, delivery_state, delivery_state_at, pr_url FROM jobs WHERE job_id = $1", job_id
    )
    assert (before["delivery_state"], seen) == ("none", ["none"])
    assert tuple(row) == ("pr_merged", "merged", WHEN, PR)


async def test_apply_refuses_a_status_the_job_cannot_move_to(db_with_migrations):
    job_id = await _job(db_with_migrations, status="failed")
    with pytest.raises(ValueError, match="failed -> pr_merged"):
        await JobDeliveryRepository(db_with_migrations).apply(job_id, lambda row: DeliveryWrite(status="pr_merged"))
    assert await db_with_migrations.fetchval("SELECT status FROM jobs WHERE job_id = $1", job_id) == "failed"


async def test_apply_writes_nothing_for_none_or_a_missing_job(db_with_migrations):
    repo = JobDeliveryRepository(db_with_migrations)
    job_id = await _job(db_with_migrations)
    assert await repo.apply(job_id, lambda row: None) is None
    assert await repo.apply("job-missing", lambda row: DeliveryWrite(status="pr_merged")) is None


async def test_reads(db_with_migrations):
    db = db_with_migrations
    repo = JobDeliveryRepository(db)
    jobs = JobsRepository(db)
    linked = await _job(db)
    await jobs.update(linked, pr_url=PR)
    unlinked = await _job(db, status="failed")
    follow_up = await _job(db, context={"ref": "embry0/x"})
    analysis = await _job(db)
    await db.execute("UPDATE jobs SET job_kind = 'analysis' WHERE job_id = $1", analysis)
    await db.execute("UPDATE jobs SET created_at = now() + interval '1 day' WHERE job_id = $1", follow_up)
    now = datetime.now(UTC)

    assert [j["job_id"] for j in await repo.find_by_pr_urls([PR.upper()])] == [linked]
    candidates = await repo.find_unlinked_in_repo(["ACME/widgets"], created_before=now, window=timedelta(days=1))
    assert [j["job_id"] for j in candidates] == [unlinked, follow_up]
    assert await repo.list_tracked(states=["pr_open"], limit=10) == [
        {"pr_url": PR, "repo": "acme/widgets", "first_created": (await jobs.get(linked))["created_at"]}
    ]
    assert await repo.repos_with_unlinked_jobs(since=now - timedelta(days=1)) == ["acme/widgets"]
    assert await repo.delivery_counts() == {"none": 3, "pr_open": 1}

    await db.execute("UPDATE jobs SET delivery_state = 'merged' WHERE job_id = $1", linked)
    assert [j["job_id"] for j in await repo.misaligned_statuses()] == [linked]


async def test_list_tracked_pages_by_keyset_so_settling_rows_cannot_hide_the_tail(db_with_migrations):
    """A sweep settles rows as it goes; a settled URL leaves the listing. With
    an offset, whatever shifted up into its place would never be looked at."""
    db = db_with_migrations
    repo = JobDeliveryRepository(db)
    for n in range(5):
        await db.execute(
            "INSERT INTO jobs (job_id, repo, task, status, pr_url, delivery_state, created_at) "
            "VALUES ($1, 'acme/widgets', 't', 'completed', $2, 'pr_open', now() + ($3 || ' minutes')::interval)",
            f"job-{n}",
            f"{PR}{n}",
            str(n),
        )

    seen: list[str] = []
    cursor = None
    for _ in range(3):
        page = await repo.list_tracked(states=["pr_open"], limit=2, after=cursor)
        if not page:
            break
        # Settle the first of each page, exactly as a sweep does.
        await db.execute("UPDATE jobs SET delivery_state = 'merged' WHERE pr_url = $1", page[0]["pr_url"])
        seen += [row["pr_url"] for row in page]
        cursor = (page[-1]["first_created"], page[-1]["pr_url"])

    # Pages are [0,1] [2,3] [4] even though 0 and 2 left the listing between
    # them: every URL is reached exactly once, none is skipped.
    assert seen == [f"{PR}{n}" for n in range(5)]
