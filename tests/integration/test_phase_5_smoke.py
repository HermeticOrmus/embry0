"""Phase 5 smoke — QA-gate routing in the issue→PR workflow.

End-to-end (with mocked agents) verification that the conditional edge added
in Phase 5 Task 5 dispatches correctly based on the ``qa.needs_qa`` flag set
by triage:

  needs_qa=True  → init → triage → plan_route → developer → review →
                   init_orchestrator → orchestrate_qa → qa_report →
                   qa_failure_bookkeeping → END
  needs_qa=False → init → triage → plan_route → developer → review → END

The plan's Task 8 Steps 1-4 (real ``gh issue create``, real embry0 job, manual
dashboard verification) are MANUAL verification covered by Task 9 (live
deployment). Step 5's unit-level failure-routing test is already covered by
``tests/unit/workflows/issue_to_pr/test_qa_failure_routing_in_triage.py``.
What's missing — and what these tests cover — is an INTEGRATION-level check
that the real compiled ``IssueToprWorkflow`` graph honours the conditional
edge end to end with mocked agent outputs.

The agent-bearing nodes (init, triage, developer, review, init_orchestrator,
orchestrate_qa, qa_report) are patched on the ``graph`` module's local
namespace BEFORE ``IssueToprWorkflow().compile()`` so the StateGraph builder
captures the stubs rather than the real implementations (which require sandbox
managers, docker clients, agent runners, MinIO, etc.). Since RAV-1152 those
nodes self-route via ``Command(goto=...)`` instead of carrying conditional
edges, so each stub mirrors the real node's routing tail — and mirrors it by
calling the real router, never by hard-coding a target. ``plan_route`` (which
resolves the route plan; here it falls back to ``LEGACY_ROUTE_PLAN`` because
the test config carries no templates repo), the bookkeeping/exhaustion nodes
(``_qa_failure_bookkeeping_node``, ``_qa_exhausted_node``) and the routing
functions (``next_route``, ``route_after_review``, ``route_after_qa_report``)
are NOT patched — they're the units under test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import Command

from embry0.workflows.issue_to_pr.route_plan import ROUTE_TARGETS, advance, next_route
from embry0.workflows.issue_to_pr.routing import route_after_review


async def _stub_init(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Skip real sandbox/docker init — just mark initialized."""
    return {"sandbox_container_id": "mock-container", "current_stage": "initialized"}


def _stub_triage_factory(needs_qa: bool, reason: str, criteria: list[str]):
    """Build a triage stub that emits a set_qa_decision-shaped qa block."""

    async def _stub_triage(state: dict[str, Any], config: RunnableConfig) -> Command[str]:
        return Command(
            goto="plan_route",
            update={
                "qa": {
                    "needs_qa": needs_qa,
                    "qa_required_reason": reason,
                    "acceptance_criteria": list(criteria),
                },
                "pipeline_config": {},
                "triage_decision": {"action": "proceed", "reasoning": "stub"},
                "current_stage": "triage_complete",
            },
        )

    return _stub_triage


async def _stub_developer(state: dict[str, Any], config: RunnableConfig) -> Command[str]:
    """developer_node self-routes along the route plan; mirror that tail."""
    updates: dict[str, Any] = {
        "pr_url": "https://github.com/owner/repo/pull/1",
        "branch_name": "embry0/test-branch",
        "current_stage": "developer_complete",
    }
    plan = state.get("route_plan") or []
    cursor = int(state.get("route_cursor", 0) or 0)
    if 0 <= cursor < len(plan) and plan[cursor].get("agent_type") == "developer":
        updates.update(advance(state))
    target = next_route({**state, **updates})
    return Command(goto=END if target == "end" else ROUTE_TARGETS[target], update=updates)


async def _stub_review(state: dict[str, Any], config: RunnableConfig) -> Command[str]:
    """review_node self-routes on the happy path by asking ``route_after_review``
    (the unit under test) where to go; mirror that tail."""
    updates: dict[str, Any] = {"current_stage": "review_passed"}
    if state.get("route_plan"):
        updates.update(advance(state))
    target = route_after_review({**state, **updates})
    if target == "developer":
        goto: Any = "retry"
    elif target == "end":
        goto = END
    else:
        goto = ROUTE_TARGETS[target]
    return Command(goto=goto, update=updates)


async def _stub_init_orchestrator(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Skip real QA orchestrator init (DinD network, profile resolve, presign, qa.yaml v2)."""
    qa = dict(state.get("qa") or {})
    qa["sandbox_token"] = "stub-token"
    qa["qa_yaml_v2_raw"] = ""
    qa["qa_yaml_v2_parsed"] = {}
    qa["head_sha"] = "abc1234"
    return {"qa": qa}


async def _stub_qa_orchestrator(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Skip the real multi-app QA orchestration; mark passed so qa_report routes to END."""
    qa = dict(state.get("qa") or {})
    qa["per_app_results"] = {}
    qa["outcome"] = "passed"
    qa["final_status"] = "passed"
    return {"qa": qa}


async def _stub_qa_report(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Skip artifact upload / GitHub check write; pass state through unchanged."""
    return {}


def _forbidden_qa_node(name: str):
    """Build a stub that fails loudly if the QA subpath is entered."""

    async def _stub(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        raise AssertionError(
            f"{name} should NOT be called when qa.needs_qa=False — route_after_review must dispatch straight to END."
        )

    return _stub


@pytest.mark.asyncio
async def test_qa_gate_routes_to_qa_when_needs_qa_true() -> None:
    """needs_qa=True path: triage sets qa.needs_qa=True; route_after_review
    dispatches to ``init_orchestrator``; the QA subpath runs; qa_report →
    bookkeeping → END (because final_status=passed routes to "end" via
    route_after_qa_report).
    """
    import embry0.workflows.issue_to_pr.graph as g_mod

    triage_stub = _stub_triage_factory(
        needs_qa=True,
        reason="frontend touched",
        criteria=["home loads"],
    )

    with (
        patch.object(g_mod, "init_node", _stub_init),
        patch.object(g_mod, "triage_node", triage_stub),
        patch.object(g_mod, "developer_node", _stub_developer),
        patch.object(g_mod, "review_node", _stub_review),
        patch.object(g_mod, "init_orchestrator_node", _stub_init_orchestrator),
        patch.object(g_mod, "qa_orchestrator_node", _stub_qa_orchestrator),
        patch.object(g_mod, "qa_report_node", _stub_qa_report),
    ):
        compiled = g_mod.IssueToprWorkflow().compile()
        initial: dict[str, Any] = {
            "job_id": "phase5-smoke-needs-qa",
            "repo": "owner/repo",
            "task": "Add a frontend widget",
        }
        config = {"configurable": {}}

        executed: list[str] = []
        async for chunk in compiled.astream(initial, config, stream_mode="updates"):
            for node in chunk:
                executed.append(node)

        final = await compiled.ainvoke(initial, config)

    # Assert the QA subpath was traversed.
    for required in ("init", "triage", "developer", "review", "init_orchestrator", "orchestrate_qa", "qa_report"):
        assert required in executed, f"missing {required!r} from {executed}"

    # Bookkeeping always runs after qa_report; it then routes to END
    # (final_status=passed → "end") so qa_exhausted should NOT appear.
    assert "qa_failure_bookkeeping" in executed, executed
    assert "qa_exhausted" not in executed, executed

    # Final state confirms triage set needs_qa and QA finished cleanly.
    assert final.get("qa", {}).get("needs_qa") is True
    assert final.get("qa", {}).get("final_status") == "passed"


@pytest.mark.asyncio
async def test_qa_gate_skips_qa_when_needs_qa_false() -> None:
    """needs_qa=False path: triage sets qa.needs_qa=False; route_after_review
    dispatches straight to END. None of init_qa / qa / qa_report should run —
    the forbidden stubs raise if reached."""
    import embry0.workflows.issue_to_pr.graph as g_mod

    triage_stub = _stub_triage_factory(
        needs_qa=False,
        reason="docs only",
        criteria=[],
    )

    with (
        patch.object(g_mod, "init_node", _stub_init),
        patch.object(g_mod, "triage_node", triage_stub),
        patch.object(g_mod, "developer_node", _stub_developer),
        patch.object(g_mod, "review_node", _stub_review),
        patch.object(g_mod, "init_orchestrator_node", _forbidden_qa_node("init_orchestrator_node")),
        patch.object(g_mod, "qa_orchestrator_node", _forbidden_qa_node("qa_orchestrator_node")),
        patch.object(g_mod, "qa_report_node", _forbidden_qa_node("qa_report_node")),
    ):
        compiled = g_mod.IssueToprWorkflow().compile()
        initial: dict[str, Any] = {
            "job_id": "phase5-smoke-skip-qa",
            "repo": "owner/repo",
            "task": "Update README",
        }
        config = {"configurable": {}}

        executed: list[str] = []
        async for chunk in compiled.astream(initial, config, stream_mode="updates"):
            for node in chunk:
                executed.append(node)

        final = await compiled.ainvoke(initial, config)

    # Happy path only: init → triage → plan_route → developer → review → END.
    assert executed == ["init", "triage", "plan_route", "developer", "review"], executed
    for forbidden in ("init_orchestrator", "orchestrate_qa", "qa_report", "qa_failure_bookkeeping", "qa_exhausted"):
        assert forbidden not in executed, f"unexpected {forbidden!r} in {executed}"

    # Final state confirms triage flagged no QA needed.
    assert final.get("qa", {}).get("needs_qa") is False
    assert "final_status" not in (final.get("qa") or {})
