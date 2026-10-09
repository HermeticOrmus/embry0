"""The PR the orchestrator opens says what the developer changed (#488).

The fallback PR body was meant to carry the developer's own summary, but in
production it never did. A production fallback PR got only the canned
"Opened by the embry0 orchestrator…" line although the developer wrote a
full summary, because:

1. ``result`` is ``_process_stream``'s merge of node updates. ``agent_outputs``
   is a reducer channel, so the merge keeps only the LAST node's fragment, the
   review's, and the developer's entry is not in it.
2. The developer writes narration and then its JSON in a fence, so
   ``json.loads(output)`` never parsed the summary even when the entry was there.

The existing test passed because it fed a full output list of bare JSON,
neither of which production produces. These tests build ``result`` the way
production does: a real graph streamed through the real ``_process_stream``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from embry0.orchestration.state import JobState
from embry0.services.issue_executor import IssueExecutor

SUMMARY = (
    "## Why\n\nAn inventory snapshot could 500 after a cache miss. `stock_cache` writes are now an upsert "
    "in their own transaction, so a key collision degrades to a cache miss."
)
NARRATION = "I'll start by locating the `stock_cache` write path and the existing REQUIRES_NEW upsert pattern."
DEVELOPER_OUTPUT = (
    f"{NARRATION}\n\nThe cache write is a plain JPA insert.\n\n"
    '```json\n{\n  "pr_url": null,\n  "branch": "embry0/iss-3bbe3dfc-eng-2468",\n'
    f'  "summary": {json.dumps(SUMMARY)},\n  "files_changed": ["StockCacheWriter.java"]\n}}\n```'
)
PR = "https://github.com/acme/app/pull/9"


# --- the developer node records its summary --------------------------------------------


async def _developer_update(output: str, *, is_error: bool = False) -> dict[str, Any]:
    import embry0.workflows.issue_to_pr.nodes as nodes

    async def _agent(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "agent_outputs": [{"agent_type": "developer", "is_error": is_error, "output": output}],
            "events": [],
            "total_cost_usd": 0.01,
        }

    sessions = AsyncMock()
    sessions.get = AsyncMock(return_value=None)
    state = {
        "job_id": "job-1",
        "issue_id": "iss-3bbe3dfc0000",
        "repo": "acme/app",
        "task": "[ENG-2468] Upsert stock_cache",
        "sandbox_container_id": "C",
        "agent_outputs": [],
        "errors": [],
        "pipeline_config": {"agent_skills": {"developer": []}, "budget_usd": 50.0},
    }
    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=AsyncMock(side_effect=_agent)),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
        patch("embry0.workflows.issue_to_pr.nodes._verify_branch_pushed", new=AsyncMock(return_value=True)),
    ):
        command = await nodes.developer_node(
            state,
            {"configurable": {"agent_runner": object(), "credentials": {}, "agent_sessions_repo": sessions}},
        )
    return command.update


@pytest.mark.asyncio
async def test_the_developer_records_the_summary_from_its_fenced_json() -> None:
    update = await _developer_update(DEVELOPER_OUTPUT)

    assert update.get("developer_summary") == SUMMARY, (
        f"the summary after the developer's narration must be recorded: {update.get('developer_summary')!r}"
    )


@pytest.mark.asyncio
async def test_a_summary_quoted_in_narration_does_not_win_over_the_developers_own() -> None:
    output = 'The template says to answer with {"summary": "one line"}.\n\n' + DEVELOPER_OUTPUT

    update = await _developer_update(output)

    assert update.get("developer_summary") == SUMMARY, update.get("developer_summary")


@pytest.mark.asyncio
async def test_an_errored_developer_records_no_summary() -> None:
    update = await _developer_update(DEVELOPER_OUTPUT, is_error=True)

    assert "developer_summary" not in update


# --- the PR fallback reads it from a result shaped like production's -------------------


def _graph(developer_update: dict[str, Any]) -> Any:
    async def developer(_state: dict[str, Any]) -> dict[str, Any]:
        return developer_update

    async def review(_state: dict[str, Any]) -> dict[str, Any]:
        return {
            "current_stage": "review_complete",
            "agent_outputs": [{"agent_type": "review", "is_error": False, "output": '{"decision": "approved"}'}],
        }

    builder = StateGraph(JobState)
    builder.add_node("developer", developer)
    builder.add_node("review", review)
    builder.add_edge(START, "developer")
    builder.add_edge("developer", "review")
    builder.add_edge("review", END)
    return builder.compile(checkpointer=InMemorySaver())


def _executor() -> IssueExecutor:
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._jobs = MagicMock()
    ex._jobs.update = AsyncMock()
    ex._jobs.get = AsyncMock(return_value={"repo": "acme/app", "task": "Upsert stock_cache", "issue_number": 7})
    ex._issues = MagicMock()
    ex._issues.update = AsyncMock()
    ex._issues.update_parent_status = AsyncMock()
    ex._db = None
    ex._audit_log_path = None
    ex._database_url = "postgresql://unused"
    ex._webhook_notifier = None
    ex._github_token = "t"
    ex._repo_prefs = None
    ex._broadcast_event = AsyncMock()  # type: ignore[method-assign]
    return ex


async def _fallback_body(developer_update: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    ex = _executor()
    graph = _graph(developer_update)
    config = {"configurable": {"thread_id": "job-1"}}
    result, interrupted, _ = await ex._process_stream(graph, {"job_id": "job-1"}, config, "iss-1", "job-1")
    assert not interrupted and result is not None
    opened = AsyncMock(return_value=PR)
    with (
        patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()),
        patch("embry0.orchestration.checkpoint.purge_thread", new=AsyncMock()),
        patch("embry0.services._executor_outcomes._open_change_request", new=opened),
        patch("embry0.services._executor_outcomes._base_branch_for_change_request", new=AsyncMock(return_value="main")),
    ):
        await ex._handle_workflow_result("iss-1", "job-1", result)
    assert opened.await_count == 1, "the fallback must open the PR for a pushed branch"
    return opened.await_args.kwargs["body"], result


@pytest.mark.asyncio
async def test_the_fallback_pr_body_carries_the_recorded_summary() -> None:
    body, result = await _fallback_body(
        {
            "branch_name": "embry0/iss-3bbe3dfc-eng-2468",
            "current_stage": "developer_complete",
            "developer_summary": SUMMARY,
            "agent_outputs": [{"agent_type": "developer", "is_error": False, "output": DEVELOPER_OUTPUT}],
        }
    )

    assert [o["agent_type"] for o in result["agent_outputs"]] == ["review"], (
        "precondition: the merged result holds only the review's fragment, as in production"
    )
    assert SUMMARY in body, f"the PR body must carry the developer's summary:\n{body}"
    assert NARRATION not in body, "narration is not a PR body"


@pytest.mark.asyncio
async def test_a_developer_entry_in_the_result_yields_its_summary_not_its_narration() -> None:
    """Old checkpoints carry no developer_summary; then the developer's own entry is read, properly."""
    from embry0.services._executor_outcomes import _developer_summary

    result = {"agent_outputs": [{"agent_type": "developer", "is_error": False, "output": DEVELOPER_OUTPUT}]}

    assert _developer_summary(result) == SUMMARY, _developer_summary(result)[:200]
