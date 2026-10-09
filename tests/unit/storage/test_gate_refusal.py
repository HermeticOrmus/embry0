"""Gate refusal vs in-run hard stop: ONE definition, two readers (#331).

A job the budget gate refused never ran and must count toward nothing. A job
that ran, spent money and was hard-stopped must count like any other. The
per-actor rate limit (BudgetSpendRepository) and the reconciler's per-ticket
attempt cap (JobsRepository.count_dispatched_by) both read that distinction
from ``gate_refusal_sql`` — these tests drive both through the same rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from embry0.safety.error_codes import ErrorCode
from embry0.storage.repositories.budget_spend import _ACTOR_COUNT_SQL, BudgetSpendRepository
from embry0.storage.repositories.gate_refusal import gate_refusal_sql
from embry0.storage.repositories.issues import IssuesRepository
from embry0.storage.repositories.jobs import JobsRepository
from embry0.storage.repositories.traces import TracesRepository

_ACTOR = "api:pinned-hard"
_N = 4


def test_the_refusal_has_its_own_stable_code():
    assert ErrorCode.BUDGET_REFUSED.value == "ERR_BUDGET_REFUSED"
    assert ErrorCode.BUDGET_OVERRUN.value == "ERR_BUDGET_OVERRUN"


@pytest.mark.asyncio
async def test_both_readers_embed_the_one_definition():
    """Asserted on the SQL each reader actually sends, not on its source."""
    from unittest.mock import AsyncMock, MagicMock

    assert gate_refusal_sql("jobs") in _ACTOR_COUNT_SQL
    db = MagicMock(fetchval=AsyncMock(return_value=0))
    await JobsRepository(db).count_dispatched_by(_ACTOR)
    assert f"NOT {gate_refusal_sql('j')}" in db.fetchval.await_args.args[0]


def test_the_definition_only_interpolates_a_plain_alias():
    with pytest.raises(ValueError):
        gate_refusal_sql("j; DROP TABLE jobs")


class _Rows:
    def __init__(self, db) -> None:
        self.db = db
        self.jobs = JobsRepository(db)
        self.issue_id: str | None = None

    async def job(self, *, code: str | None, trace: bool = False, overrun_usd: float = 0.0) -> str:
        if self.issue_id is None:
            self.issue_id = await IssuesRepository(self.db).create(
                title="ENG-1", repo="o/r", tracker="linear", tracker_key="ENG-1"
            )
        job_id: str = await self.jobs.create(repo="o/r", task="t", issue_id=self.issue_id, dispatched_by=_ACTOR)
        await self.jobs.update(job_id, status="running")  # the runner marks running BEFORE it asks the gate
        if trace:
            await TracesRepository(self.db).create(
                job_id=job_id, agent_type="developer", model="m", result="fail", cost_usd=0.4
            )
        if code is not None:
            await self.jobs.update(job_id, status="failed", error_code=code, budget_overrun_usd=overrun_usd)
        return job_id

    async def counts(self) -> tuple[int, int, int]:
        """(actor rate-limit count, reconciler actor count, reconciler per-ticket count)"""
        since = datetime.now(UTC) - timedelta(hours=1)
        return (
            await BudgetSpendRepository(self.db).dispatch_count_since(_ACTOR, since),
            await self.jobs.count_dispatched_by(_ACTOR, since=since),
            await self.jobs.count_dispatched_by(_ACTOR, tracker="linear", tracker_key="ENG-1"),
        )


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_n_in_run_hard_stops_count_toward_the_actors_limit(db_with_migrations):
    """#331: an API caller pins budget_overrun_mode=hard with a tiny budget.
    Each job runs an agent, spends, is stopped — and used to be invisible."""
    rows = _Rows(db_with_migrations)
    for _ in range(_N):
        await rows.job(code="ERR_BUDGET_OVERRUN", trace=True, overrun_usd=0.15)
    assert await rows.counts() == (_N, _N, _N)


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_n_gate_refusals_count_toward_nothing(db_with_migrations):
    rows = _Rows(db_with_migrations)
    for _ in range(_N):
        await rows.job(code="ERR_BUDGET_REFUSED")
    assert await rows.counts() == (0, 0, 0)


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_hard_stop_whose_trace_was_never_written_still_counts(db_with_migrations):
    """The edge the trace heuristic could not see: the trace write is
    best-effort. The runner records the overrun figure on the row, and a
    refusal never has one."""
    rows = _Rows(db_with_migrations)
    await rows.job(code="ERR_BUDGET_OVERRUN", trace=False, overrun_usd=0.15)
    assert await rows.counts() == (1, 1, 1)


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_rows_from_before_the_code_existed_are_read_by_the_old_evidence(db_with_migrations):
    """Historical refusals carry ERR_BUDGET_OVERRUN and are left as they are:
    no trace and no overrun figure means the gate refused it."""
    rows = _Rows(db_with_migrations)
    await rows.job(code="ERR_BUDGET_OVERRUN")  # legacy refusal
    await rows.job(code="ERR_BUDGET_OVERRUN")  # legacy refusal
    await rows.job(code="ERR_BUDGET_OVERRUN", trace=True)  # legacy in-run stop, written before #246 kept the figure
    assert await rows.counts() == (1, 1, 1)


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_every_other_outcome_counts(db_with_migrations):
    rows = _Rows(db_with_migrations)
    await rows.job(code=None)  # still running
    await rows.job(code="ERR_UNKNOWN")
    await rows.job(code="ERR_AGENT_TIMEOUT", trace=True)
    assert await rows.counts() == (3, 3, 3)
