"""The orchestrator drains on shutdown instead of killing its jobs (#307).

At SIGTERM the executor stops starting jobs, lets the running ones reach a
node boundary (or a deadline), stamps them ``interrupted_at`` and kills the
agent each left in its sandbox. Boot then resumes or relaunches them.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.config import Embry0Config
from embry0.orchestration.restart import ExecutorDraining, ShutdownInterrupt
from embry0.services.issue_executor import IssueExecutor


def _executor(limit: int = 0) -> IssueExecutor:
    jobs = MagicMock()
    jobs.append_log_event = AsyncMock(return_value=1)
    jobs.record_launch = AsyncMock()
    jobs.mark_interrupted = AsyncMock(side_effect=lambda ids: list(ids))
    sandbox = MagicMock()
    sandbox.stop_agent = AsyncMock(return_value=True)
    return IssueExecutor(
        issues_repo=MagicMock(),
        jobs_repo=jobs,
        traces_repo=MagicMock(),
        workflow_registry=MagicMock(),
        database_url="postgresql://unused/unused",
        config=Embry0Config(_env_file=None, max_running_jobs=limit),
        sandbox_manager=sandbox,
    )


class _Runs:
    """Stands in for ``_run_workflow``: records each start, then waits until
    released. ``stop_at_boundary`` jobs raise ShutdownInterrupt once the
    executor drains, as ``_process_stream`` does at a node boundary."""

    def __init__(self, ex: IssueExecutor, stop_at_boundary: set[str] | None = None) -> None:
        self.ex = ex
        self.started: list[str] = []
        self.stop_at_boundary = stop_at_boundary or set()
        self.release = asyncio.Event()

    def __call__(self, issue_id: Any, job_id: str, issue: Any, additional_context: str = "") -> Any:
        async def run() -> None:
            self.started.append(job_id)
            while not self.release.is_set():
                if job_id in self.stop_at_boundary and self.ex.draining:
                    raise ShutdownInterrupt(job_id)
                await asyncio.sleep(0.01)

        return run()


async def _settle() -> None:
    for _ in range(25):
        await asyncio.sleep(0)


async def test_a_draining_executor_starts_no_job_and_keeps_its_launch_record() -> None:
    ex = _executor()
    runs = _Runs(ex)
    ex._run_workflow = runs  # type: ignore[method-assign]

    await ex.drain(timeout=1)
    task = ex.submit_job("job-late", repo="o/r", title="dispatched during shutdown")
    await asyncio.wait_for(task, timeout=2)

    assert runs.started == [], f"no job may start once the executor drains; started {runs.started}"
    launch = ex._jobs.record_launch.await_args_list
    assert [c.args[0] for c in launch] == ["job-late"], "the deferred job must record how to start it at the next boot"
    assert launch[0].args[1]["runner"] == "workflow" and launch[0].args[1]["issue"]["title"] == (
        "dispatched during shutdown"
    )
    ex._jobs.mark_interrupted.assert_any_await(["job-late"])


async def test_a_job_waiting_for_a_run_slot_never_starts_once_draining() -> None:
    ex = _executor(limit=1)
    runs = _Runs(ex, stop_at_boundary={"job-running"})
    ex._run_workflow = runs  # type: ignore[method-assign]
    ex.submit_job("job-running", repo="o/r", title="t")
    ex.submit_job("job-queued", repo="o/r", title="t")
    await _settle()
    assert runs.started == ["job-running"]

    stamped = await ex.drain(timeout=2)

    assert runs.started == ["job-running"], f"the queued job must not start when the slot frees; {runs.started}"
    assert sorted(stamped) == ["job-queued", "job-running"]


async def test_a_running_job_stops_at_its_next_node_boundary_without_failing() -> None:
    ex = _executor()
    publish = AsyncMock()
    ex._event_bus = MagicMock(publish=publish)
    runs = _Runs(ex, stop_at_boundary={"job-1"})
    ex._run_workflow = runs  # type: ignore[method-assign]
    ex.submit_job("job-1", repo="o/r", title="t")
    await _settle()

    stamped = await asyncio.wait_for(ex.drain(timeout=5), timeout=3)

    assert stamped == ["job-1"], f"the stopped job must be stamped interrupted; stamped {stamped}"
    published = [c.args[1].get("type") for c in publish.await_args_list]
    assert "job_failed" not in published, f"a job stopped for a shutdown has not failed; published {published}"
    ex._sandbox.stop_agent.assert_awaited_once_with("sandbox-job-1")
    messages = [c.args[1].get("message", "") for c in ex._jobs.append_log_event.await_args_list]
    assert any("Interrupted" in m for m in messages), f"the job's timeline must say it was interrupted: {messages}"


async def test_a_job_still_inside_a_node_at_the_deadline_is_cancelled_and_stamped() -> None:
    ex = _executor()
    runs = _Runs(ex)  # never reaches a boundary on its own
    ex._run_workflow = runs  # type: ignore[method-assign]
    task = ex.submit_job("job-slow", repo="o/r", title="t")
    await _settle()

    stamped = await ex.drain(timeout=0.05)

    assert task.cancelled(), "a job past the drain deadline must be cancelled, not waited on"
    assert stamped == ["job-slow"]
    ex._sandbox.stop_agent.assert_awaited_once_with("sandbox-job-slow")


async def test_a_finished_job_is_not_stamped() -> None:
    ex = _executor()
    runs = _Runs(ex)
    ex._run_workflow = runs  # type: ignore[method-assign]
    ex.submit_job("job-done", repo="o/r", title="t")
    await _settle()
    runs.release.set()
    await _settle()
    ex._jobs.mark_interrupted = AsyncMock(return_value=[])  # the row reads completed

    assert await ex.drain(timeout=1) == []
    ex._sandbox.stop_agent.assert_not_awaited()


async def test_a_resume_is_refused_while_draining() -> None:
    ex = _executor()
    await ex.drain(timeout=0)

    with pytest.raises(ExecutorDraining):
        ex.schedule_resume("iss-1", "job-1", {"q": "a"}, kind="resume_via_answer")


class _Graph:
    """Replays canned astream events; counts how many were consumed."""

    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.consumed = 0

    async def astream(self, input_value: Any, config: Any = None, stream_mode: Any = None) -> Any:
        for event in self.events:
            self.consumed += 1
            yield event

    async def aget_state(self, config: Any) -> None:
        return None


async def test_a_drain_stops_a_job_only_after_a_node_finishes_not_mid_node() -> None:
    """Custom events stream from inside a running node; stopping there would
    throw away a node that could still finish within the drain."""
    ex = _executor()
    ex._jobs.update = AsyncMock()

    async def broadcast(job_id: str, event: dict[str, Any]) -> None:
        ex._draining = True  # SIGTERM arrives while the developer is mid-turn

    ex._broadcast_event = broadcast  # type: ignore[method-assign]
    graph = _Graph(
        [
            ("custom", {"type": "agent_text"}),
            ("updates", {"developer": {"current_stage": "developer_complete"}}),
            ("updates", {"review": {"current_stage": "review_complete"}}),
        ]
    )

    with pytest.raises(ShutdownInterrupt):
        await ex._process_stream(graph, {}, {}, "iss-1", "job-1", stop_on_drain=True)

    assert graph.consumed == 2, (
        f"the job must stop right after the node that was running finished; consumed {graph.consumed}"
    )
    ex._jobs.update.assert_awaited_once_with("job-1", current_stage="developer_complete")


async def test_qa_and_onboard_streams_are_not_stopped_by_a_drain() -> None:
    ex = _executor()
    ex._jobs.update = AsyncMock()
    ex._draining = True
    graph = _Graph([("updates", {"qa": {"current_stage": "qa_complete"}})])

    final, interrupted, _ = await ex._process_stream(graph, {}, {}, "", "job-qa")

    assert final == {"current_stage": "qa_complete"} and not interrupted
