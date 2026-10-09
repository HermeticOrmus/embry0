"""A resumed run reports its own cost, not the session's running total (#501).

claude-code 2.1.277+ saves a session's cost totals at exit and restores them on
a headless resume, so the resumed run's ``total_cost_usd`` includes every
earlier run. The job total already counted those (``run_agent_node`` adds each
run's ``cost_usd``), so passing the CLI total through counts them twice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from embry0.agents.claude_cli_session import sanitize_cwd_for_session_dir
from embry0.agents.executor import SdkAgentExecutor
from embry0.agents.invocation import AgentInvocation
from embry0.safety.policy import default_policy_for_agent

SESSION_ID = "2cde0b11-1cda-47f6-8a45-fb99f2126f24"


def _inv() -> AgentInvocation:
    return AgentInvocation(
        agent_type="developer",
        prompt="continue",
        system_prompt="",
        system_context="",
        model="claude-sonnet-4-6",
        tools=["Read"],
        skills=[],
        mcp_servers={},
        max_turns=5,
        timeout_seconds=60,
        execution_mode="sdk",
        auth_mode="oauth",
        safety_policy=default_policy_for_agent("developer"),
        channel_config=None,
    )


def _stage_session(home: Path, restored_total: float | None) -> None:
    """The session file AgentRunner stages before a resume, as 2.1.292 left it."""
    session_dir = home / ".claude" / "projects" / sanitize_cwd_for_session_dir("/workspace")
    session_dir.mkdir(parents=True)
    lines = [json.dumps({"type": "user", "sessionId": SESSION_ID, "message": {"role": "user", "content": "first"}})]
    if restored_total is not None:
        lines.append(json.dumps({"type": "cost-state", "sessionId": SESSION_ID, "totalCostUSD": restored_total}))
    (session_dir / f"{SESSION_ID}.jsonl").write_text("\n".join(lines) + "\n")


async def _stream(total_cost_usd: float):
    yield AssistantMessage(content=[TextBlock(text="second")], model="claude-sonnet-4-6", session_id=SESSION_ID)
    yield ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=SESSION_ID,
        total_cost_usd=total_cost_usd,
        usage={"input_tokens": 1000, "output_tokens": 100},
        result="second",
    )


async def _resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, restored: float | None, total: float):
    home = tmp_path / "home"
    _stage_session(home, restored)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EMBRY0_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    events: list[dict[str, Any]] = []
    with patch("claude_agent_sdk.query", return_value=_stream(total)):
        out = await SdkAgentExecutor().run(
            _inv(), {"configurable": {}, "_test_writer": events.append}, resume_session_id=SESSION_ID
        )
    return out, events


async def test_a_resumed_run_reports_only_its_own_cost(tmp_path, monkeypatch) -> None:
    """Measured on 2.1.292: run 1 cost 0.0045; its resume reported total_cost_usd 0.009."""
    out, events = await _resume(tmp_path, monkeypatch, restored=0.0045, total=0.009)

    assert out.is_error is False, out.error_message
    assert out.cost_usd == pytest.approx(0.0045), (
        f"the resumed session's restored cost was counted again: {out.cost_usd} (CLI total 0.009, restored 0.0045)"
    )
    cost_events = [event["cost_usd"] for event in events if event.get("type") == "cost_update"]
    assert cost_events == [pytest.approx(0.0045)], cost_events


async def test_a_session_without_saved_totals_keeps_the_cli_total(tmp_path, monkeypatch) -> None:
    """A session an older CLI wrote has nothing to restore: the total is the run's own."""
    out, _ = await _resume(tmp_path, monkeypatch, restored=None, total=0.0045)
    assert out.cost_usd == pytest.approx(0.0045)


async def test_a_total_below_the_saved_one_is_taken_as_the_runs_own(tmp_path, monkeypatch) -> None:
    """A CLI that did not restore the totals reports less than the saved one; nothing to subtract."""
    out, _ = await _resume(tmp_path, monkeypatch, restored=0.02, total=0.0045)
    assert out.cost_usd == pytest.approx(0.0045)
