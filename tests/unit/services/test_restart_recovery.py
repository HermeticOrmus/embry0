"""Boot starts the jobs it took back after a restart (#307): a checkpointed job
goes on from its checkpoint, a job that never got going starts again from the
launch record on its row."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from embry0.api.app import _start_recovered_jobs
from embry0.execution.sandbox_manager import SandboxReenrollError
from embry0.safety.error_codes import ErrorCode
from embry0.services.issue_executor import IssueExecutor


def _executor() -> IssueExecutor:
    jobs = MagicMock()
    jobs.append_log_event = AsyncMock(return_value=1)
    jobs.record_launch = AsyncMock()
    jobs.update = AsyncMock()
    jobs.get = AsyncMock(return_value={"status": "running"})
    jobs.fail_jobs = AsyncMock(return_value=[])
    issues = MagicMock()
    issues.update = AsyncMock()
    issues.get = AsyncMock(return_value={"id": "iss-1", "repo": "o/r", "title": "Fix it"})
    ex = IssueExecutor(
        issues_repo=issues,
        jobs_repo=jobs,
        traces_repo=MagicMock(total_cost_for_job=AsyncMock(return_value=0.0)),
        workflow_registry=MagicMock(),
        database_url="postgresql://unused/unused",
    )
    ex._post_tracker_outcome = AsyncMock()  # type: ignore[method-assign]
    ex._cleanup_sandbox = AsyncMock()  # type: ignore[method-assign]
    return ex


async def test_a_resumed_job_goes_on_from_its_checkpoint_without_touching_its_status() -> None:
    ex = _executor()
    ex._execute_workflow_stream = AsyncMock(return_value=({"current_stage": "done"}, False, None))  # type: ignore[method-assign]
    ex._handle_workflow_result = AsyncMock()  # type: ignore[method-assign]

    await ex.resume_after_restart({"job_id": "job-1", "issue_id": "iss-1", "action": "resume"})

    assert ex._execute_workflow_stream.await_args.args[0] is None, (
        "a job resumed after a restart has no answer: it goes on from its checkpoint (input None), "
        f"got {ex._execute_workflow_stream.await_args.args[0]!r}"
    )
    status_writes = [c for c in ex._jobs.update.await_args_list if "status" in c.kwargs]
    assert status_writes == [], f"the job already reads running; the resume must not rewrite it: {status_writes}"
    ex._handle_workflow_result.assert_awaited_once_with("iss-1", "job-1", {"current_stage": "done"})


async def test_a_resume_after_restart_whose_sandbox_cannot_be_re_enrolled_fails_instead_of_pausing() -> None:
    """A paused job goes back to paused on a re-enroll failure; a job that was
    running has no pause to go back to."""
    ex = _executor()
    ex._execute_workflow_stream = AsyncMock(  # type: ignore[method-assign]
        side_effect=SandboxReenrollError("proxy down", error_code=ErrorCode.SANDBOX_INIT)
    )
    ex._open_fallback_pr = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]

    await ex.resume_after_restart({"job_id": "job-1", "issue_id": "iss-1", "action": "resume"})

    statuses = [c.kwargs.get("status") for c in ex._jobs.update.await_args_list if "status" in c.kwargs]
    assert statuses == ["failed"], f"the job must fail, not pause; status writes {statuses}"


async def test_a_qa_job_is_launched_again_with_its_recorded_arguments() -> None:
    ex = _executor()
    ex._run_qa_workflow = AsyncMock()  # type: ignore[method-assign]
    launch = {
        "runner": "qa",
        "repo": "o/r",
        "branch": "feature",
        "qa_overrides": {"force_all_apps": True},
        "agent_models": {"qa": "m"},
    }

    await ex.relaunch({"job_id": "job-qa", "status": "pending", "launch": launch, "action": "relaunch"})

    ex._run_qa_workflow.assert_awaited_once_with(
        "job-qa", repo="o/r", branch="feature", qa_overrides={"force_all_apps": True}, agent_models={"qa": "m"}
    )
    assert not ex._jobs.record_launch.await_count, "a relaunch must not rewrite the launch record it came from"


async def test_an_issue_job_is_launched_again_from_its_issue_row() -> None:
    ex = _executor()
    ex._run_workflow = AsyncMock()  # type: ignore[method-assign]
    launch = {"runner": "workflow", "issue_id": "iss-1", "additional_context": "Q: a? A: b"}

    await ex.relaunch({"job_id": "job-1", "status": "pending", "issue_id": "iss-1", "launch": launch})

    ex._run_workflow.assert_awaited_once_with(
        "iss-1", "job-1", {"id": "iss-1", "repo": "o/r", "title": "Fix it"}, additional_context="Q: a? A: b"
    )


async def test_a_job_whose_issue_is_gone_fails_instead_of_starting() -> None:
    ex = _executor()
    ex._issues.get = AsyncMock(return_value=None)
    ex._run_workflow = AsyncMock()  # type: ignore[method-assign]

    await ex.relaunch({"job_id": "job-1", "launch": {"runner": "workflow", "issue_id": "iss-gone"}})

    ex._run_workflow.assert_not_awaited()
    assert ex._jobs.fail_jobs.await_args.args[:2] == (["job-1"], ErrorCode.ORPHANED.value)


async def test_boot_starts_each_recovered_job_by_its_action() -> None:
    executor = MagicMock()
    jobs: list[dict[str, Any]] = [
        {"job_id": "a", "action": "resume"},
        {"job_id": "b", "action": "relaunch"},
    ]

    _start_recovered_jobs(executor, jobs)
    await asyncio.sleep(0)

    executor.resume_after_restart.assert_called_once_with(jobs[0])
    executor.relaunch.assert_called_once_with(jobs[1])
