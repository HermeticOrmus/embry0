"""A graph that loops without reaching END stops after 25 steps (#498).

LangGraph's default ``recursion_limit`` was 25 up to 1.0.5 and is 10007 since
1.1.4. embry0 never set it, so on LangGraph 1.x the stop behind
ERR_GRAPH_RECURSION would all but vanish: a looping graph would run on until
its budget ran out. ``IssueExecutor._build_graph_config`` now sets it.

Every test here runs a looping ``StateGraph(JobState)`` through the real
``_execute_workflow_stream``, which builds its config with the real
``_build_graph_config``. Nothing in this file passes ``recursion_limit``.
Only the checkpointer (in memory) and the workflow choice are swapped. The
loop opens an exit after ``_EXIT_AFTER`` steps, so a tree without the stop
finishes in milliseconds and fails on the step count instead of running
10007 steps.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from embry0.orchestration.state import JobState
from embry0.safety.error_codes import ErrorCode, classify_failure
from embry0.services.issue_executor import IssueExecutor

# Steps one run gets before the stop: LangGraph's own default up to 1.0.5,
# and so the limit every embry0 job ran under until this upgrade.
_LIMIT = 25
# A resume re-runs the node that paused and counts from the paused
# checkpoint's step, so the looping resume below gets two more node runs.
# Measured the same on LangGraph 0.6.11 (default 25) and 1.2.14 (25 set).
_RESUME_STEPS = _LIMIT + 2
# Far enough past the limit to tell "stopped" from "finished", far short of 10007.
_EXIT_AFTER = 60


class _LoopingWorkflow:
    """``developer`` and ``review`` hand the job back and forth until ``_EXIT_AFTER`` steps.

    With ``ask_first`` the job pauses on a question before the loop, so the
    loop runs in the resume.
    """

    def __init__(self, *, ask_first: bool = False) -> None:
        self.steps = 0
        self._ask_first = ask_first

    def _node(self, name: str):
        async def node(state: dict[str, Any]) -> dict[str, Any]:
            self.steps += 1
            return {"current_stage": f"{name}_complete"}

        return node

    async def _ask(self, state: dict[str, Any]) -> dict[str, Any]:
        self.steps += 1
        interrupt({"reason": "ask_user", "questions": [{"id": "q-1", "question": "Which branch?"}]})
        return {"current_stage": "answered"}

    def _next(self, target: str):
        return lambda _state: END if self.steps >= _EXIT_AFTER else target

    def compile(self, config: dict[str, Any]) -> Any:
        builder = StateGraph(JobState)
        builder.add_node("developer", self._node("developer"))
        builder.add_node("review", self._node("review"))
        builder.add_conditional_edges("developer", self._next("review"))
        builder.add_conditional_edges("review", self._next("developer"))
        if self._ask_first:
            builder.add_node("ask", self._ask)
            builder.add_edge(START, "ask")
            builder.add_edge("ask", "developer")
        else:
            builder.add_edge(START, "developer")
        return builder.compile(checkpointer=config["checkpointer"])


def _executor(workflow: _LoopingWorkflow) -> IssueExecutor:
    """A real executor, so ``_build_graph_config`` is the production one."""
    jobs = MagicMock()
    jobs.get = AsyncMock(return_value={"trace_id": None})
    jobs.update = AsyncMock()
    issues = MagicMock()
    issues.get = AsyncMock(return_value=None)
    issues.update = AsyncMock()
    traces = MagicMock()
    traces.total_cost_for_job = AsyncMock(return_value=0.0)
    ex = IssueExecutor(
        issues_repo=issues,
        jobs_repo=jobs,
        traces_repo=traces,
        workflow_registry=MagicMock(),
        database_url="postgresql://unused",
    )
    ex._select_workflow = lambda _pipeline: workflow  # type: ignore[method-assign]
    return ex


@pytest.fixture
def saver():
    """One in-memory checkpointer for every run of a test, as Postgres would be."""
    memory = InMemorySaver()

    @asynccontextmanager
    async def context(_url: str):
        yield memory

    with patch("embry0.services._executor_run.checkpointer_context", context):
        yield memory


def _stopped_message(workflow: _LoopingWorkflow, where: str, expected: int = _LIMIT) -> str:
    return (
        f"{where}: the looping graph ran {workflow.steps} steps "
        f"({'finished through its test exit' if workflow.steps >= _EXIT_AFTER else 'then stopped'}); "
        f"the recursion stop must end it after {expected}. "
        "_build_graph_config must set recursion_limit: LangGraph 1.x defaults to 10007."
    )


async def test_a_looping_run_stops_at_the_recursion_limit(saver) -> None:
    workflow = _LoopingWorkflow()
    ex = _executor(workflow)

    raised: BaseException | None = None
    try:
        await ex._execute_workflow_stream({"job_id": "job-1", "repo": "o/r"}, "iss-1", "job-1")
    except GraphRecursionError as exc:
        raised = exc

    assert workflow.steps == _LIMIT, _stopped_message(workflow, "fresh run")
    assert raised is not None, "the stop must raise GraphRecursionError, which ERR_GRAPH_RECURSION is mapped from"
    code = classify_failure(exc=raised, stage="developer_complete")
    assert code is ErrorCode.GRAPH_RECURSION, (
        f"LangGraph's own {type(raised).__module__}.{type(raised).__qualname__} must classify as "
        f"ERR_GRAPH_RECURSION, got {code}"
    )


async def test_a_looping_resume_stops_at_the_recursion_limit(saver) -> None:
    """A resume is a run of its own, with its own step budget from the same config."""
    workflow = _LoopingWorkflow(ask_first=True)
    ex = _executor(workflow)

    _, interrupted, _ = await ex._execute_workflow_stream({"job_id": "job-1", "repo": "o/r"}, "iss-1", "job-1")
    assert interrupted, "the job must pause on its question before the loop"

    workflow.steps = 0
    raised: BaseException | None = None
    try:
        await ex._execute_workflow_stream(Command(resume={"q-1": "main"}), "iss-1", "job-1")
    except GraphRecursionError as exc:
        raised = exc

    assert workflow.steps == _RESUME_STEPS, _stopped_message(workflow, "resume", _RESUME_STEPS)
    assert raised is not None, "the stop must raise GraphRecursionError on a resume too"


async def test_a_looping_job_fails_with_err_graph_recursion(saver) -> None:
    """The whole issue-to-PR runner: the stop ends the job, and the row says why."""
    workflow = _LoopingWorkflow()
    ex = _executor(workflow)

    with (
        patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()),
        patch("embry0.orchestration.checkpoint.purge_thread", new=AsyncMock()),
        patch.object(ex, "_handle_workflow_result", new=AsyncMock()) as finished,
    ):
        await ex._run_workflow("iss-1", "job-1", {"title": "Loop forever", "body": "", "repo": "o/r"})

    assert workflow.steps == _LIMIT, _stopped_message(workflow, "issue-to-PR job")
    finished.assert_not_awaited()
    terminal = ex._jobs.update.await_args.kwargs
    assert terminal.get("status") == "failed", f"a looping job must fail, the last job update was {terminal}"
    assert terminal.get("error_code") == ErrorCode.GRAPH_RECURSION.value, (
        f"a looping job must fail with ERR_GRAPH_RECURSION, the job row got {terminal.get('error_code')!r}"
    )
