"""RAV-1281 — QA-loop graph nodes must return deltas, not the full state.

``JobState`` puts ``operator.add`` reducers on ``agent_outputs`` and
``errors`` (the #95 sweep), so a node that returns the whole state dict
re-appends the entire accumulated history on every pass. The two nodes in
the triage↔QA failure loop did exactly that, doubling both lists each QA
failure round. These tests drive each node through a real
``StateGraph(JobState)`` so the reducer merge is exercised, not mocked.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph

from embry0.orchestration.state import JobState
from embry0.safety.error_codes import ErrorCode
from embry0.workflows.issue_to_pr.graph import _qa_exhausted_node, _qa_failure_bookkeeping_node


def _graph_with(node: Any) -> Any:
    builder = StateGraph(JobState)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return builder.compile()


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "agent_outputs": [{"agent_type": "developer", "output": "x"}],
        "errors": ["e1"],
        "qa": {"final_status": "failed", "failure_rounds": 0},
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_bookkeeping_failed_round_does_not_duplicate_history() -> None:
    out = await _graph_with(_qa_failure_bookkeeping_node).ainvoke(_state())

    assert len(out["agent_outputs"]) == 1, "reducer re-append: full-state return doubled agent_outputs"
    assert out["errors"] == ["e1"]
    assert out["qa"]["failure_rounds"] == 1
    assert out["qa"]["final_status"] == "failed"


@pytest.mark.asyncio
async def test_bookkeeping_passed_round_advances_cursor_without_duplication() -> None:
    plan = [
        {"agent_type": "developer", "node_id": "developer", "config": {}},
        {"agent_type": "qa", "node_id": "qa", "config": {}},
        {"agent_type": "output", "node_id": "output", "config": {}},
    ]
    out = await _graph_with(_qa_failure_bookkeeping_node).ainvoke(
        _state(
            qa={"final_status": "passed", "needs_qa": True},
            route_plan=plan,
            route_cursor=1,
        )
    )

    assert len(out["agent_outputs"]) == 1
    assert out["errors"] == ["e1"]
    assert out["route_cursor"] == 2


@pytest.mark.asyncio
async def test_bookkeeping_exhausted_round_is_noop() -> None:
    out = await _graph_with(_qa_failure_bookkeeping_node).ainvoke(_state(qa={"final_status": "exhausted"}))

    assert len(out["agent_outputs"]) == 1
    assert out["errors"] == ["e1"]
    assert out["qa"] == {"final_status": "exhausted"}


@pytest.mark.asyncio
async def test_qa_exhausted_sets_terminal_shape_without_duplication() -> None:
    out = await _graph_with(_qa_exhausted_node).ainvoke(_state(qa={"final_status": "failed", "failure_rounds": 2}))

    assert len(out["agent_outputs"]) == 1
    assert out["errors"] == ["e1"]
    assert out["qa"]["final_status"] == "exhausted"
    assert out["qa"]["failure_rounds"] == 2  # merge_qa_block keeps sibling keys
    assert out["error_code"] == ErrorCode.QA_FAILURES_UNRESOLVED.value
