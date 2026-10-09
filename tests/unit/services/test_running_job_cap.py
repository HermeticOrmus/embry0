"""The global cap on jobs running at once (#583).

Jobs past ``MAX_RUNNING_JOBS`` wait in ``pending`` (the board's "queued"
lane) and start on their own when a running job's runner returns, whether it
finished, failed, was cancelled or paused for an answer.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.config import Embry0Config
from embry0.services.issue_executor import IssueExecutor


def _executor(limit: int) -> IssueExecutor:
    jobs = MagicMock()
    jobs.append_log_event = AsyncMock(return_value=1)
    return IssueExecutor(
        issues_repo=MagicMock(),
        jobs_repo=jobs,
        traces_repo=MagicMock(),
        workflow_registry=MagicMock(),
        database_url="postgresql://unused/unused",
        config=Embry0Config(_env_file=None, max_running_jobs=limit),
    )


class _Runs:
    """A stand-in for ``_run_workflow``: each run records its start and then
    blocks until the test finishes it (or makes it raise)."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self._gates: dict[str, asyncio.Event] = {}
        self._errors: dict[str, BaseException] = {}

    def _gate(self, job_id: str) -> asyncio.Event:
        return self._gates.setdefault(job_id, asyncio.Event())

    def finish(self, job_id: str, error: BaseException | None = None) -> None:
        if error is not None:
            self._errors[job_id] = error
        self._gate(job_id).set()

    def finish_all(self) -> None:
        for job_id in list(self._gates):
            self._gate(job_id).set()

    def __call__(self, issue_id: Any, job_id: str, issue: Any, additional_context: str = "") -> Any:
        async def run() -> None:
            self.started.append(job_id)
            await self._gate(job_id).wait()
            if job_id in self._errors:
                raise self._errors[job_id]

        return run()


async def _settle() -> None:
    for _ in range(25):
        await asyncio.sleep(0)


def _queued_messages(ex: IssueExecutor, job_id: str) -> list[str]:
    return [
        call.args[1].get("message", "")
        for call in ex._jobs.append_log_event.await_args_list
        if call.args[0] == job_id and call.args[1].get("type") == "system"
    ]


async def _drain(ex: IssueExecutor, runs: _Runs) -> None:
    runs.finish_all()
    await _settle()
    await asyncio.gather(*list(ex._background_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_job_past_the_cap_queues_then_starts_when_a_slot_frees():
    ex = _executor(limit=2)
    runs = _Runs()
    ex._run_workflow = runs  # type: ignore[method-assign]

    for job_id in ("job-1", "job-2", "job-3"):
        ex.submit_job(job_id, repo="o/r", title=job_id)
    await _settle()

    assert runs.started == ["job-1", "job-2"], f"with MAX_RUNNING_JOBS=2 only two jobs may run; started {runs.started}"
    queued = _queued_messages(ex, "job-3")
    assert queued and "Queued" in queued[0], f"job-3 should tell its timeline it is queued, got {queued}"

    runs.finish("job-1")
    await _settle()
    assert runs.started == ["job-1", "job-2", "job-3"], (
        f"job-3 should start once job-1 frees its slot; started {runs.started}"
    )
    await _drain(ex, runs)


@pytest.mark.asyncio
async def test_no_cap_by_default():
    assert Embry0Config(_env_file=None).max_running_jobs == 0, "the default must not cap existing deployments"
    ex = IssueExecutor(
        issues_repo=MagicMock(),
        jobs_repo=MagicMock(append_log_event=AsyncMock(return_value=1)),
        traces_repo=MagicMock(),
        workflow_registry=MagicMock(),
        database_url="postgresql://unused/unused",
        config=Embry0Config(_env_file=None),
    )
    runs = _Runs()
    ex._run_workflow = runs  # type: ignore[method-assign]

    for n in range(6):
        ex.submit_job(f"job-{n}", repo="o/r", title="t")
    await _settle()

    assert len(runs.started) == 6, f"with no cap every job should start at once; started {runs.started}"
    await _drain(ex, runs)


@pytest.mark.asyncio
async def test_a_failed_run_frees_its_slot():
    ex = _executor(limit=1)
    runs = _Runs()
    ex._run_workflow = runs  # type: ignore[method-assign]

    ex.submit_job("job-1", repo="o/r", title="t")
    ex.submit_job("job-2", repo="o/r", title="t")
    await _settle()
    assert runs.started == ["job-1"], f"only one job may run; started {runs.started}"

    runs.finish("job-1", error=RuntimeError("boom"))
    await _settle()
    assert runs.started == ["job-1", "job-2"], f"a raising run must still free its slot; started {runs.started}"
    await _drain(ex, runs)


@pytest.mark.asyncio
async def test_cancelling_a_queued_job_never_runs_it_and_keeps_the_queue_moving():
    ex = _executor(limit=1)
    runs = _Runs()
    ex._run_workflow = runs  # type: ignore[method-assign]

    for job_id in ("job-1", "job-2", "job-3"):
        ex.submit_job(job_id, repo="o/r", title="t")
    await _settle()

    # cancel_job cancels exactly this task; the rest of it is DB bookkeeping.
    ex._tasks_by_job["job-2"].cancel()
    await _settle()
    runs.finish("job-1")
    await _settle()

    assert runs.started == ["job-1", "job-3"], (
        f"a job cancelled while queued must never run, and must not hold a slot; started {runs.started}"
    )
    await _drain(ex, runs)


@pytest.mark.asyncio
async def test_a_resume_waits_for_a_slot_ahead_of_fresh_jobs():
    """A paused job holds no slot (its runner returned), so its resume has to
    take one. It goes ahead of fresh jobs: its sandbox already exists and
    someone has just answered it. The job reads ``running`` while it waits, as
    every resume always has: a restart then fails it as orphaned, visibly,
    instead of leaving an answered job waiting for an answer."""
    ex = _executor(limit=1)
    runs = _Runs()
    ex._run_workflow = runs  # type: ignore[method-assign]
    ex._jobs.get = AsyncMock(return_value={"status": "paused"})
    ex._jobs.update = AsyncMock()
    ex._issues.get = AsyncMock(return_value=None)
    ex._issues.update = AsyncMock()
    ex._post_tracker_outcome = AsyncMock()  # type: ignore[method-assign]
    ex._cleanup_sandbox = AsyncMock()  # type: ignore[method-assign]

    async def _stream(input_value: Any, issue_id: Any, job_id: str, **_: Any) -> Any:
        runs.started.append(job_id)
        await runs._gate(job_id).wait()
        return None, False, None

    ex._execute_workflow_stream = _stream  # type: ignore[method-assign]

    ex.submit_job("job-1", repo="o/r", title="t")
    await _settle()
    ex.submit_job("job-2", repo="o/r", title="t")
    ex.schedule_resume(None, "job-r", {"choice": "retry"}, kind="resume_via_api")
    await _settle()

    assert runs.started == ["job-1"], f"the resume must wait for a slot; started {runs.started}"
    ex._jobs.update.assert_any_await("job-r", status="running")
    assert _queued_messages(ex, "job-r"), "a waiting resume should tell its timeline it is queued"

    runs.finish("job-1")
    await _settle()
    assert runs.started == ["job-1", "job-r"], f"the resume should take the freed slot first; started {runs.started}"

    runs.finish("job-r")
    await _settle()
    assert runs.started == ["job-1", "job-r", "job-2"], f"the fresh job runs after the resume; started {runs.started}"
    await _drain(ex, runs)
