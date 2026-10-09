"""#298: a branch job never QAs a `target: deployed` app.

The deployed site does not contain the branch under test, so its verdict says
nothing about the change (ERR_QA_FAILURES_UNRESOLVED, 7 jobs). Standalone QA
keeps deployed targets; every QA run inside the issue-to-PR graph drops them.
"""

from __future__ import annotations

import pytest

from embry0.workflows.qa.orchestrator import qa_orchestrator_node

_ALL_DEPLOYED = """
version: 2
qa_required: always
defaults:
  sandbox_profile: qa-external
apps:
  web:
    target: deployed
    frontend_url: "http://app.internal.example:8080/"
    ready_checks:
      - http: "http://app.internal.example:8080/health"
        expect_status: [200, 302]
"""


def _no_subtasks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(resolved, **kw):
        raise AssertionError(f"QA sub-task started for {resolved.app_name!r}")

    monkeypatch.setattr("embry0.workflows.qa.orchestrator_helpers.run_subtask", _boom)


def _state(**qa: object) -> dict:
    return {
        "job_id": "33333333-3333-3333-3333-333333333333",
        "repo": "org/deployed-repo",
        "branch_name": "embry0/job-x",
        "qa": {"qa_yaml_v2_raw": _ALL_DEPLOYED, "changed_files": ["frontend/a.tsx"], **qa},
    }


@pytest.mark.asyncio
async def test_the_flag_drops_deployed_apps_even_when_qa_is_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_subtasks(monkeypatch)
    out = await qa_orchestrator_node(_state(exclude_deployed_apps=True), {"configurable": {}})
    qa = out["qa"]
    assert qa["apps_to_qa"] == []
    assert qa["final_status"] == "passed"
    assert any("web" in w and "deployed" in w for w in qa["validation_warnings"])


@pytest.mark.asyncio
async def test_the_issue_to_pr_graph_sets_the_flag_on_every_qa_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever routed into QA — verify, triage's rerun_qa, a template without
    a review step — the graph's own QA node carries the rule."""
    _no_subtasks(monkeypatch)
    from embry0.workflows.issue_to_pr.graph import _branch_qa_orchestrator_node

    out = await _branch_qa_orchestrator_node(_state(), {"configurable": {}})
    assert out["qa"]["apps_to_qa"] == []
    assert out["qa"]["exclude_deployed_apps"] is True


def test_the_issue_to_pr_graph_uses_the_branch_variant_and_standalone_qa_does_not() -> None:
    from embry0.workflows.issue_to_pr.graph import IssueToprWorkflow, _branch_qa_orchestrator_node
    from embry0.workflows.qa.graph import QAWorkflow

    assert IssueToprWorkflow().compile().nodes["orchestrate_qa"].bound.afunc is _branch_qa_orchestrator_node
    assert QAWorkflow().compile().nodes["orchestrate_qa"].bound.afunc is qa_orchestrator_node
