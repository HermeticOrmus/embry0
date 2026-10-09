"""SH-P4.4: the reviewer is told the task, its acceptance criteria and the logged failure."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from embry0.workflows.issue_to_pr.log_tool import CONTEXT_LOGS_TOOL

_EVIDENCE = {
    "source": "case",
    "case_id": "hc-1",
    "signature": "lead-contacts 502 no JSON object",
    "samples": ["acme-backend logged 4x: 502 Bad Gateway"],
    "services": ["acme-backend"],
}

_TASK = """[ENG-1344] fix(ml): lead-contacts extraction returns truncated JSON

## Evidence

acme-backend 502 for jane@example.com, Authorization: Bearer abcdef0123456789

## Acceptance criteria
- [ ] a lead with 40 contacts extracts
"""


def _sessions_repo():
    repo = AsyncMock()
    repo.get = AsyncMock(return_value=None)
    repo.upsert = AsyncMock()
    return repo


def _state(**extra):
    return {
        "job_id": "J",
        "issue_id": "iss",
        "repo": "o/r",
        "task": _TASK,
        "branch_name": "embry0/x",
        "sandbox_container_id": "C",
        "agent_outputs": [],
        "errors": [],
        "pipeline_config": {"agent_skills": {"developer": []}, "budget_usd": 50.0},
        **extra,
    }


async def _run(node_name: str, state: dict, output: str, **configurable):
    import embry0.workflows.issue_to_pr.nodes as nodes

    captured: dict = {}

    async def _agent(*args, **kwargs):
        captured.update(kwargs)
        return {
            "agent_outputs": [{"agent_type": kwargs["agent_type"], "is_error": False, "output": output}],
            "events": [],
            "total_cost_usd": 0.01,
        }

    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=AsyncMock(side_effect=_agent)),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
        patch("embry0.workflows.issue_to_pr.nodes._verify_branch_pushed", new=AsyncMock(return_value=True)),
    ):
        await getattr(nodes, node_name)(
            state,
            {
                "configurable": {
                    "agent_runner": object(),
                    "credentials": {},
                    "agent_sessions_repo": _sessions_repo(),
                    **configurable,
                }
            },
        )
    return captured


_REVIEW_OK = '{"decision": "approved", "validation": {}, "review_comments": [], "summary": "ok"}'


# ---- reviewer -------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_sees_task_criteria_and_evidence() -> None:
    captured = await _run("review_node", _state(case_evidence=_EVIDENCE), _REVIEW_OK)
    prompt = captured["prompt"]
    assert "Task the developer was given:" in prompt
    assert "lead-contacts extraction returns truncated JSON" in prompt
    assert "Acceptance criteria:\n- [ ] a lead with 40 contacts extracts" in prompt
    assert "Signature: lead-contacts 502 no JSON object" in prompt
    assert "actually addresses the logged failure" in prompt
    assert '"addresses_logged_failure"' in prompt
    # Everything before the schema marker — the resumed-session delta cuts there.
    assert prompt.index("Task the developer was given:") < prompt.index("Return ONLY a JSON object:")
    # The reviewer's provider never had the raw ticket before: it gets it redacted.
    assert "jane@example.com" not in prompt and "abcdef0123456789" not in prompt
    # The reviewer is NOT given the log tool.
    assert CONTEXT_LOGS_TOOL not in captured["agent_definition"]["tools"]


@pytest.mark.asyncio
async def test_reviewer_without_evidence_still_sees_the_task() -> None:
    captured = await _run("review_node", _state(task="Add a dark mode toggle"), _REVIEW_OK)
    prompt = captured["prompt"]
    assert "Task the developer was given:\nAdd a dark mode toggle" in prompt
    assert "Acceptance criteria: none stated" in prompt
    assert "Logged failure" not in prompt
    assert "addresses_logged_failure" not in prompt


@pytest.mark.asyncio
async def test_reviewer_is_told_to_scope_its_test_run_to_the_change() -> None:
    # 2026-09-27: a reviewer that started the whole suite in a sandbox with no
    # database burned its budget on ConnectionRefused and ended with no verdict.
    from embry0.workflows.issue_to_pr.nodes._review import REVIEW_TIMEOUT_SECONDS

    captured = await _run("review_node", _state(), _REVIEW_OK)
    prompt = captured["prompt"]
    flat = " ".join(prompt.split())
    assert "Run the tests that cover the change" in flat
    assert "Do NOT run the whole suite" in flat
    assert f"{REVIEW_TIMEOUT_SECONDS} seconds in total" in flat, "the budget told to the reviewer must be the real one"
    assert captured["timeout_seconds"] == REVIEW_TIMEOUT_SECONDS
    assert prompt.rstrip().endswith("The JSON object is the last thing you output — nothing after it.")
    assert prompt.index("Return ONLY a JSON object:") < prompt.index("The JSON object is the last thing"), (
        "the closing instruction must ride after the schema marker, so a resumed session's delta prompt keeps it"
    )
    assert "Run the test suite (if tests exist)" not in prompt


@pytest.mark.asyncio
async def test_reviewer_task_is_bounded() -> None:
    captured = await _run("review_node", _state(task="word " * 10_000), _REVIEW_OK)
    assert len(captured["prompt"]) < 12_000
    assert "[task truncated]" in captured["prompt"]
