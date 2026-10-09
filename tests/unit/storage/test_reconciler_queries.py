"""The durable reads behind the Linear reconciler's bounds and healing (#284)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from embry0.storage.repositories.issues import IssuesRepository
from embry0.storage.repositories.jobs import JobsRepository

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

_ACTOR = "linear-reconciler"


async def _tracked(issues: IssuesRepository, key: str, tracker: str = "linear") -> str:
    issue_id: str = await issues.create(title=key, repo="o/r", tracker=tracker, tracker_key=key)
    return issue_id


async def test_count_dispatched_by_narrows_by_actor_window_and_ticket(db_with_migrations):
    issues, jobs = IssuesRepository(db_with_migrations), JobsRepository(db_with_migrations)
    one, two = await _tracked(issues, "ENG-1"), await _tracked(issues, "ENG-2")
    for issue_id, actor in [(one, _ACTOR), (one, _ACTOR), (one, "alice"), (two, _ACTOR)]:
        await jobs.create(repo="o/r", task="t", issue_id=issue_id, dispatched_by=actor)
    old = await jobs.create(repo="o/r", task="t", issue_id=two, dispatched_by=_ACTOR)
    await db_with_migrations.execute("UPDATE jobs SET created_at = NOW() - INTERVAL '2 days' WHERE job_id = $1", old)
    # A failed job is still an attempt: the bound is on jobs started.
    failed = await jobs.create(repo="o/r", task="t", issue_id=two, dispatched_by=_ACTOR)
    await jobs.update(failed, status="running")
    await jobs.update(failed, status="failed")

    day_ago = datetime.now(UTC) - timedelta(days=1)
    assert await jobs.count_dispatched_by(_ACTOR) == 5
    assert await jobs.count_dispatched_by(_ACTOR, since=day_ago) == 4
    assert await jobs.count_dispatched_by(_ACTOR, tracker="linear", tracker_key="ENG-1") == 2
    assert await jobs.count_dispatched_by(_ACTOR, tracker="linear", tracker_key="ENG-2") == 3
    assert await jobs.count_dispatched_by(_ACTOR, tracker="gitlab", tracker_key="ENG-1") == 0
    assert await jobs.count_dispatched_by("nobody") == 0


async def test_a_gate_refusal_burns_no_attempt_but_an_in_run_hard_stop_does(db_with_migrations):
    """Both end `failed` + ERR_BUDGET_OVERRUN; only one of them did any work (#331)."""
    from embry0.storage.repositories.traces import TracesRepository

    issues, jobs = IssuesRepository(db_with_migrations), JobsRepository(db_with_migrations)
    issue_id = await _tracked(issues, "ENG-1")

    async def _budget_stopped(*, ran_an_agent: bool) -> None:
        job_id = await jobs.create(repo="o/r", task="t", issue_id=issue_id, dispatched_by=_ACTOR)
        await jobs.update(job_id, status="running")  # the runner marks running BEFORE it asks the gate
        if ran_an_agent:
            await TracesRepository(db_with_migrations).create(
                job_id=job_id, agent_type="developer", model="m", result="fail", cost_usd=3.0
            )
        await jobs.update(job_id, status="failed", error_code="ERR_BUDGET_OVERRUN")

    await _budget_stopped(ran_an_agent=False)
    await _budget_stopped(ran_an_agent=False)
    assert await jobs.count_dispatched_by(_ACTOR, tracker="linear", tracker_key="ENG-1") == 0
    assert await jobs.count_dispatched_by(_ACTOR) == 0

    await _budget_stopped(ran_an_agent=True)
    assert await jobs.count_dispatched_by(_ACTOR, tracker="linear", tracker_key="ENG-1") == 1
    assert await jobs.count_dispatched_by(_ACTOR) == 1


async def _age(db, issue_id: str, minutes: int) -> None:
    await db.execute(
        "UPDATE issues SET updated_at = NOW() - make_interval(mins => $2) WHERE id = $1", issue_id, minutes
    )


async def test_stale_triaging_finds_only_old_jobless_rows_of_the_tracker(db_with_migrations):
    db = db_with_migrations
    issues, jobs = IssuesRepository(db), JobsRepository(db)
    stale = await _tracked(issues, "ENG-1")
    fresh = await _tracked(issues, "ENG-2")
    live = await _tracked(issues, "ENG-3")
    dead_job = await _tracked(issues, "ENG-4")
    other_tracker = await _tracked(issues, "7", tracker="gitlab")
    still_open = await _tracked(issues, "ENG-5")
    for issue_id in (stale, fresh, live, dead_job, other_tracker):
        await issues.update(issue_id, status="triaging")
    await jobs.create(repo="o/r", task="t", issue_id=live)  # pending = live
    finished = await jobs.create(repo="o/r", task="t", issue_id=dead_job)
    await jobs.update(finished, status="running")
    await jobs.update(finished, status="failed")
    for issue_id in (stale, live, dead_job, other_tracker, still_open):
        await _age(db, issue_id, 20)

    reported = await issues.stale_triaging("linear", 15, reopen=False)
    assert sorted(r["tracker_key"] for r in reported) == ["ENG-1", "ENG-4"]
    assert (await issues.get(stale))["status"] == "triaging"  # reporting changes nothing

    reopened = await issues.stale_triaging("linear", 15, reopen=True)
    assert sorted(r["tracker_key"] for r in reopened) == ["ENG-1", "ENG-4"]
    statuses = {key: (await issues.get(i))["status"] for key, i in [("stale", stale), ("dead", dead_job)]}
    assert statuses == {"stale": "open", "dead": "open"}
    for untouched in (fresh, live, other_tracker):
        assert (await issues.get(untouched))["status"] == "triaging"
    assert await issues.stale_triaging("linear", 15, reopen=True) == []
