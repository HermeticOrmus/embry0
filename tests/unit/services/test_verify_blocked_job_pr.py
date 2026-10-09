"""A change the gate blocked still gets its pull request (#341 review).

The orchestrator's PR fallback only ran for COMPLETED jobs, so a job the
verification gate failed — with its branch pushed and a verdict to publish —
left the work stranded and the verdict with nowhere to go: the same
"failed, no PR" shape as the seven jobs this work exists to fix.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embry0.safety.error_codes import ErrorCode
from embry0.services.issue_executor import IssueExecutor


def _executor() -> IssueExecutor:
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._jobs = MagicMock()
    ex._jobs.update = AsyncMock()
    ex._jobs.get = AsyncMock(return_value={"repo": "acme/app", "task": "Fix the idle timer", "issue_number": 7})
    ex._issues = MagicMock()
    ex._issues.update = AsyncMock()
    ex._issues.update_parent_status = AsyncMock()
    ex._db = None
    ex._audit_log_path = None
    ex._database_url = "postgresql://unused"
    ex._webhook_notifier = None
    ex._github_token = "t"
    ex._repo_prefs = None
    return ex


async def _run(result: dict[str, Any]) -> tuple[IssueExecutor, MagicMock]:
    ex = _executor()
    opened = AsyncMock(return_value="https://github.com/acme/app/pull/9")
    with (
        patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()),
        patch("embry0.orchestration.checkpoint.purge_thread", new=AsyncMock()),
        patch("embry0.services._executor_outcomes._open_change_request", new=opened),
        patch("embry0.services._executor_outcomes._base_branch_for_change_request", new=AsyncMock(return_value="main")),
    ):
        await ex._handle_workflow_result("iss-1", "job-1", result)
    return ex, opened


def _blocked(**extra: Any) -> dict[str, Any]:
    return {
        "triage_decision": {"action": "proceed"},
        "current_stage": "verify_failed",
        "error_code": ErrorCode.VERIFY_NEW_TEST_FAILURES.value,
        "branch_name": "embry0/job-1",
        "agent_outputs": [{"agent_type": "review", "is_error": False, "output": "approved"}],
        **extra,
    }


@pytest.mark.asyncio
async def test_a_gate_blocked_job_gets_a_pull_request() -> None:
    ex, opened = await _run(_blocked())
    opened.assert_awaited_once()
    assert opened.await_args.kwargs["branch"] == "embry0/job-1"
    assert opened.await_args.kwargs["labels"] == ["embry0"]
    kwargs = ex._jobs.update.await_args.kwargs
    assert kwargs["status"] == "failed"
    assert kwargs["error_code"] == ErrorCode.VERIFY_NEW_TEST_FAILURES.value
    assert kwargs["pr_url"] == "https://github.com/acme/app/pull/9"


@pytest.mark.asyncio
async def test_the_pr_body_tells_the_reader_why_the_job_stopped() -> None:
    _, opened = await _run(_blocked())
    body = opened.await_args.kwargs["body"]
    assert "Closes #7" in body
    assert "automated tests" in body.lower()
    for forbidden in ("ERR_", "Traceback", "/workspace"):
        assert forbidden not in body


@pytest.mark.asyncio
async def test_a_job_that_already_has_a_pr_is_left_alone() -> None:
    _, opened = await _run(_blocked(pr_url="https://github.com/acme/app/pull/4"))
    opened.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_abandoned_job_still_gets_no_fallback_pr() -> None:
    """#349 widened the fallback to every failed job; a person's abandon is
    the one exception (#410), because they decided the work is not wanted."""
    _, opened = await _run(_blocked(error_code=ErrorCode.ABANDONED.value, current_stage="abandoned"))
    opened.assert_not_awaited()
