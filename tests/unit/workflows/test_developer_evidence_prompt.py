"""SH-P4.4: the developer gets the case evidence and, when confirmed, a scoped log tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from embry0.workflows.issue_to_pr.log_tool import CONTEXT_LOGS_TOOL, grant_log_tool

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


# ---- the grant ------------------------------------------------------------


def test_grant_appends_without_touching_the_stored_definition() -> None:
    stored = {"tools": ["Read", "Bash"], "mcp_servers": {"other": {"type": "stdio"}}}
    merged = grant_log_tool(stored)
    assert merged["tools"] == ["Read", "Bash", CONTEXT_LOGS_TOOL]
    assert set(merged["mcp_servers"]) == {"other", "context"}
    assert stored == {"tools": ["Read", "Bash"], "mcp_servers": {"other": {"type": "stdio"}}}
    assert grant_log_tool(merged)["tools"].count(CONTEXT_LOGS_TOOL) == 1


# ---- developer ------------------------------------------------------------


@pytest.mark.asyncio
async def test_developer_with_confirmed_scope_gets_the_tool_and_is_told_its_limits() -> None:
    from types import SimpleNamespace

    captured = await _run(
        "developer_node",
        _state(case_evidence=_EVIDENCE, case_log_containers=["acme-backend"]),
        "...",
        embry0_config=SimpleNamespace(context_proxy_query_quota=7),
    )
    definition = captured["agent_definition"]
    assert CONTEXT_LOGS_TOOL in definition["tools"]
    assert "context" in definition["mcp_servers"]
    prompt = captured["prompt"]
    assert "Signature: lead-contacts 502 no JSON object" in prompt
    assert "| acme-backend logged 4x: 502 Bad Gateway" in prompt
    assert CONTEXT_LOGS_TOOL in prompt and "acme-backend" in prompt and "7 queries" in prompt
    assert "never copy log content" in prompt.lower()


@pytest.mark.asyncio
async def test_developer_with_evidence_but_no_confirmed_scope_gets_no_tool() -> None:
    captured = await _run("developer_node", _state(case_evidence=_EVIDENCE), "...")
    assert CONTEXT_LOGS_TOOL not in captured["agent_definition"]["tools"]
    assert captured["agent_definition"]["mcp_servers"] == {}
    assert "Signature: lead-contacts" in captured["prompt"]
    assert CONTEXT_LOGS_TOOL not in captured["prompt"]


@pytest.mark.asyncio
async def test_developer_without_a_case_is_unchanged() -> None:
    captured = await _run("developer_node", _state(), "...")
    assert CONTEXT_LOGS_TOOL not in captured["agent_definition"]["tools"]
    assert "Logged failure" not in captured["prompt"]
    assert CONTEXT_LOGS_TOOL not in captured["prompt"]


@pytest.mark.asyncio
async def test_watcher_flag_never_grants_the_developer_the_tool() -> None:
    """watcher_log_tool is the ANALYSIS agent's unscoped grant."""
    captured = await _run("developer_node", _state(watcher_log_tool=True), "...")
    assert CONTEXT_LOGS_TOOL not in captured["agent_definition"]["tools"]
