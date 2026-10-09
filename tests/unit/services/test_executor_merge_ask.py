"""A completed job's PR is handed to the merge-approval ask (#319 phase 2)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embry0.services.issue_executor import IssueExecutor

PR = "https://github.com/acme/backend/pull/42"
CLEAN_RUN = {"agent_outputs": [{"agent_type": "review", "is_error": False}]}


def _executor():
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._jobs = MagicMock()
    ex._jobs.update = AsyncMock()
    ex._jobs.get = AsyncMock(return_value={"repo": "acme/backend", "task": "Fix it"})
    ex._issues = MagicMock()
    ex._issues.update = AsyncMock()
    ex._issues.update_parent_status = AsyncMock()
    ex._db = None
    ex._audit_log_path = None
    ex._database_url = "postgresql://unused"
    ex._webhook_notifier = None
    ex._merge_approvals = MagicMock()
    return ex


async def _run(ex, result):
    with (
        patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()),
        patch("embry0.orchestration.checkpoint.purge_thread", new=AsyncMock()),
        patch("embry0.services._executor_outcomes.record_case_event", new=AsyncMock()),
        patch("embry0.services._executor_outcomes._publish_verification", new=AsyncMock()),
    ):
        await ex._handle_workflow_result("iss-1", "job-1", result)


@pytest.mark.asyncio
async def test_a_completed_job_with_a_pr_is_asked_about():
    ex = _executor()
    await _run(ex, {**CLEAN_RUN, "pr_url": PR})
    assert ex._jobs.update.await_args.kwargs["status"] == "completed"
    ex._merge_approvals.schedule_ask.assert_called_once_with(job_id="job-1", pr_url=PR)


@pytest.mark.asyncio
async def test_a_failed_job_is_not_asked_about_even_with_a_pr():
    ex = _executor()
    await _run(ex, {"current_stage": "failed", "error_code": "ERR_X", "pr_url": PR})
    assert ex._jobs.update.await_args.kwargs["status"] == "failed", "the fixture did not produce a failed job"
    ex._merge_approvals.schedule_ask.assert_not_called()


@pytest.mark.asyncio
async def test_a_job_without_a_pr_is_not_asked_about():
    ex = _executor()
    await _run(ex, CLEAN_RUN)
    ex._merge_approvals.schedule_ask.assert_not_called()


@pytest.mark.asyncio
async def test_a_failing_ask_never_touches_the_job_outcome():
    ex = _executor()
    ex._merge_approvals.schedule_ask.side_effect = RuntimeError("boom")
    await _run(ex, {**CLEAN_RUN, "pr_url": PR})
    ex._issues.update.assert_awaited()
