"""RAV-1291 — the job's cost row must count QA, on every finalization path.

The QA fan-out runs as a sub-graph that never propagates per-agent cost to the
parent workflow state, so ``result["total_cost_usd"]`` is short by the whole QA
spend. Trace rows carry it. The standalone-job path already resolved cost from
traces; the issue-backed path did not, and 46 jobs under-reported by $37.57
before the two numbers were compared.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embry0.services._cost import resolve_job_total_cost
from embry0.services.issue_executor import IssueExecutor


def _executor(traces_total: float | None = None):
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._jobs = MagicMock()
    ex._jobs.update = AsyncMock()
    ex._jobs.get = AsyncMock(return_value=None)
    ex._issues = MagicMock()
    ex._issues.update = AsyncMock()
    ex._issues.update_parent_status = AsyncMock()
    ex._db = None
    ex._audit_log_path = None
    ex._database_url = "postgresql://unused"
    ex._webhook_notifier = None
    if traces_total is not None:
        ex._traces = MagicMock()
        ex._traces.total_cost_for_job = AsyncMock(return_value=traces_total)
    return ex


async def _finalize(ex, result):
    with (
        patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()),
        patch("embry0.orchestration.checkpoint.purge_thread", new=AsyncMock()),
    ):
        await ex._handle_workflow_result("iss-1", "job-1", result)
    return ex._jobs.update.await_args.kwargs


def _ok(agent):
    return {"agent_type": agent, "is_error": False, "output": "done"}


def _completed_result(state_cost: float):
    return {
        "triage_decision": {"action": "proceed"},
        "current_stage": "output_finalized",
        "errors": [],
        "agent_outputs": [_ok("triage"), _ok("developer"), _ok("review"), _ok("qa")],
        "pr_url": "https://github.com/o/r/pull/1",
        "result_summary": "ok",
        "total_cost_usd": state_cost,
    }


# ---- the helper ----------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_sum_wins_over_state_cost():
    traces = MagicMock()
    traces.total_cost_for_job = AsyncMock(return_value=4.33)
    assert await resolve_job_total_cost(traces, "job-1", 2.48) == 4.33


@pytest.mark.asyncio
async def test_state_cost_survives_when_no_traces_exist():
    """A job that died before writing a trace row must not be zeroed out."""
    traces = MagicMock()
    traces.total_cost_for_job = AsyncMock(return_value=0.0)
    assert await resolve_job_total_cost(traces, "job-1", 2.48) == 2.48


@pytest.mark.asyncio
async def test_missing_traces_repo_falls_back_to_state_cost():
    assert await resolve_job_total_cost(None, "job-1", 1.5) == 1.5


# ---- the issue-backed finalization path ----------------------------------


@pytest.mark.asyncio
async def test_issue_backed_job_persists_the_qa_inclusive_total():
    """The live shape: state says $2.48 (triage+dev+review), traces say $4.33
    because QA cost $1.86. The row must record what was actually spent."""
    ex = _executor(traces_total=4.33)
    kwargs = await _finalize(ex, _completed_result(2.48))
    assert kwargs["status"] == "completed"
    assert kwargs["total_cost_usd"] == 4.33


@pytest.mark.asyncio
async def test_issue_backed_job_keeps_state_cost_without_traces():
    ex = _executor(traces_total=0.0)
    kwargs = await _finalize(ex, _completed_result(2.48))
    assert kwargs["total_cost_usd"] == 2.48
