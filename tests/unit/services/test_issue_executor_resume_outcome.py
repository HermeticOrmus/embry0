"""RAV-1406 — resumed jobs post the tracker terminal-outcome comment.

``_run_workflow`` posts the outcome in its ``finally``; ``resume()`` had no
hook at all, so any Linear-dispatched job that paused and was resumed
finished silently (pilot job-347244e88769: dispatch comment, no outcome
comment). These tests pin the ``finally`` placement: the hook (#163:
``_post_tracker_outcome``) must fire on completion, on failure, and on a
repeat interrupt (its internal gate no-ops for non-terminal statuses in
production).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.services.issue_executor import IssueExecutor


def _executor():
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._jobs = MagicMock()
    ex._jobs.update = AsyncMock()
    ex._jobs.get = AsyncMock(return_value={"status": "completed"})
    ex._issues = MagicMock()
    ex._issues.update = AsyncMock()
    ex._handle_workflow_result = AsyncMock()
    ex._handle_interrupt = AsyncMock()
    ex._cleanup_sandbox = AsyncMock()
    ex._post_tracker_outcome = AsyncMock()
    return ex


@pytest.mark.asyncio
async def test_resume_posts_tracker_outcome_on_completion():
    ex = _executor()
    ex._execute_workflow_stream = AsyncMock(return_value=({"current_stage": "done"}, False, None))

    await ex.resume("iss-1", "job-1", {"choice": "continue_retrying"})

    ex._post_tracker_outcome.assert_awaited_once_with("iss-1", "job-1")


@pytest.mark.asyncio
async def test_resume_posts_tracker_outcome_when_stream_raises():
    ex = _executor()
    ex._execute_workflow_stream = AsyncMock(side_effect=RuntimeError("boom"))

    await ex.resume("iss-1", "job-1", {"choice": "continue_retrying"})

    # The failure path persisted the job as failed AND still posted the outcome.
    assert ex._jobs.update.await_args.kwargs.get("status") == "failed"
    ex._post_tracker_outcome.assert_awaited_once_with("iss-1", "job-1")


@pytest.mark.asyncio
async def test_resume_posts_outcome_hook_even_on_repeat_interrupt():
    # The finally fires on the early-return interrupt path too; in production
    # the hook's internal gate no-ops while the job is merely paused again.
    ex = _executor()
    ex._execute_workflow_stream = AsyncMock(return_value=(None, True, {"reason": "max_retries"}))

    await ex.resume(None, "job-2", {"choice": "continue_retrying"})

    ex._handle_interrupt.assert_awaited_once()
    ex._post_tracker_outcome.assert_awaited_once_with(None, "job-2")
