"""Tests that triage parse failure and agent error route to END without reaching developer."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing
from langgraph.graph import END
from langgraph.types import Command


@pytest.fixture()
def base_state() -> dict[str, Any]:
    return {
        "job_id": "job-test-001",
        "repo": "owner/repo",
        "task": "Fix the thing",
        "issue_number": 42,
        "issue_id": "iss-001",
        "sandbox_container_id": "container-abc",
        "agent_outputs": [],
        "errors": [],
        "retry_count": 0,
        "total_cost_usd": 0.0,
    }


@pytest.fixture()
def mock_config() -> dict[str, Any]:
    return {
        "configurable": {
            "agent_runner": MagicMock(),
            "credentials": {},
            "repo_preferences_repo": None,
        }
    }


@pytest.mark.asyncio
async def test_triage_parse_error_routes_to_end(
    base_state: dict[str, Any],
    mock_config: dict[str, Any],
) -> None:
    """TriageParseError must return Command(goto=END), not a plain dict."""
    from embry0.workflows.issue_to_pr.nodes import triage_node

    agent_output = {
        "agent_outputs": [{"agent_type": "triage", "is_error": False, "output": "NOT VALID JSON <<<"}],
        "total_cost_usd": 0.01,
        "current_stage": "triage_complete",
    }

    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=AsyncMock(return_value=agent_output)),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
    ):
        result = await triage_node(base_state, mock_config)

    assert isinstance(result, Command), f"Expected Command, got {type(result)}"
    assert result.goto == END
    assert result.update.get("current_stage") == "failed"
    assert "ERR_TRIAGE_MALFORMED" in str(result.update.get("error_code", ""))


@pytest.mark.asyncio
async def test_triage_agent_error_routes_to_end(
    base_state: dict[str, Any],
    mock_config: dict[str, Any],
) -> None:
    """Agent is_error=True must route to END without reaching developer node."""
    from embry0.workflows.issue_to_pr.nodes import triage_node

    agent_output = {
        "agent_outputs": [
            {
                "agent_type": "triage",
                "is_error": True,
                "error_message": "Sandbox exec failed",
                "output": "",
            }
        ],
        "total_cost_usd": 0.0,
        "current_stage": "triage_failed",
    }

    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=AsyncMock(return_value=agent_output)),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
    ):
        result = await triage_node(base_state, mock_config)

    assert isinstance(result, Command)
    assert result.goto == END
    assert result.update.get("current_stage") == "failed"


@pytest.mark.asyncio
async def test_triage_empty_agent_outputs_routes_to_end(
    base_state: dict[str, Any],
    mock_config: dict[str, Any],
) -> None:
    """Empty agent_outputs must route to END."""
    from embry0.workflows.issue_to_pr.nodes import triage_node

    agent_output: dict[str, Any] = {
        "agent_outputs": [],
        "total_cost_usd": 0.0,
        "current_stage": "triage_complete",
    }

    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=AsyncMock(return_value=agent_output)),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
    ):
        result = await triage_node(base_state, mock_config)

    assert isinstance(result, Command)
    assert result.goto == END


@pytest.mark.asyncio
async def test_triage_agent_error_log_includes_code_and_result(
    base_state: dict[str, Any],
    mock_config: dict[str, Any],
) -> None:
    """The failure log used to carry only the SDK sentence. Code and result text
    have to be on the same event, redacted, while the job still sees the raw message."""
    from embry0.workflows.issue_to_pr.nodes import triage_node

    token = "ghp_" + "a1B2" * 9
    message = f"API Error: 529 overloaded {token}"
    failed = {
        "agent_outputs": [
            {
                "agent_type": "triage",
                "is_error": True,
                "error_message": message,
                "error_code": "ERR_UPSTREAM_HTTP",
                "output": "no triage decision",
            }
        ],
        "total_cost_usd": 0.0,
        "current_stage": "triage_failed",
    }

    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=AsyncMock(return_value=failed)),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
        structlog.testing.capture_logs() as logs,
    ):
        result = await triage_node(base_state, mock_config)

    event = next(entry for entry in logs if entry["event"] == "triage_agent_error")
    assert event["error_code"] == "ERR_UPSTREAM_HTTP"
    assert "529" in event["error_message"]
    assert token not in event["error_message"]
    assert token not in event["error"]
    assert "***REDACTED***" in event["error_message"]
    assert event["result"] == "no triage decision"
    assert isinstance(result, Command)
    assert token in result.update["errors"][0]


# ---- #455: one schema slip gets one repair turn -------------------------------

_MISSING_CONFIDENCE = '{"action": "proceed", "job_kind": "analysis", "reasoning": "stack trace points at the cache"}'
_VALID = (
    '{"action": "proceed", "confidence": 0.8, "job_kind": "analysis", "reasoning": "stack trace points at the cache"}'
)


def _answer(text: str, cost: float) -> dict[str, Any]:
    return {
        "agent_outputs": [{"agent_type": "triage", "is_error": False, "output": text}],
        "total_cost_usd": cost,
        "current_stage": "triage_complete",
    }


@pytest.mark.asyncio
async def test_a_decision_missing_a_required_field_is_repaired_once(
    base_state: dict[str, Any],
    mock_config: dict[str, Any],
) -> None:
    """The 2026-10-01 shape: a valid answer that left out `confidence`."""
    from embry0.workflows.issue_to_pr.nodes import triage_node

    run = AsyncMock(side_effect=[_answer(_MISSING_CONFIDENCE, 0.41), _answer(_VALID, 0.45)])
    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=run),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
    ):
        result = await triage_node(base_state, mock_config)

    assert isinstance(result, Command)
    assert result.update.get("current_stage") != "failed", (
        f"one missing field still failed the job: {result.update.get('errors')}"
    )
    assert result.goto != END
    assert result.update["triage_decision"]["confidence"] == 0.8
    assert run.await_count == 2
    repair_prompt = run.await_args_list[1].kwargs["prompt"]
    assert "confidence" in repair_prompt and "Field required" in repair_prompt, repair_prompt
    assert _MISSING_CONFIDENCE in repair_prompt, "the repair turn must quote the rejected answer"
    assert result.update["total_cost_usd"] == pytest.approx(0.45), "the repair run's cost was dropped"


@pytest.mark.asyncio
async def test_a_second_invalid_answer_fails_the_job_and_there_is_no_third(
    base_state: dict[str, Any],
    mock_config: dict[str, Any],
) -> None:
    from embry0.workflows.issue_to_pr.nodes import triage_node

    run = AsyncMock(side_effect=[_answer(_MISSING_CONFIDENCE, 0.41), _answer("still not JSON", 0.43)])
    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=run),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
    ):
        result = await triage_node(base_state, mock_config)

    assert run.await_count == 2, f"triage was asked {run.await_count} times"
    assert result.goto == END
    assert result.update.get("error_code") == "ERR_TRIAGE_MALFORMED"
    assert "Invalid JSON" in result.update["errors"][0], "the error must be the repair answer's, not the first"


@pytest.mark.asyncio
async def test_an_errored_repair_run_fails_the_job_with_the_original_error(
    base_state: dict[str, Any],
    mock_config: dict[str, Any],
) -> None:
    from embry0.workflows.issue_to_pr.nodes import triage_node

    errored = {
        "agent_outputs": [{"agent_type": "triage", "is_error": True, "error_message": "boom", "output": ""}],
        "total_cost_usd": 0.42,
    }
    run = AsyncMock(side_effect=[_answer(_MISSING_CONFIDENCE, 0.41), errored])
    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=run),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
    ):
        result = await triage_node(base_state, mock_config)

    assert run.await_count == 2
    assert result.goto == END
    assert result.update.get("error_code") == "ERR_TRIAGE_MALFORMED"
    assert "confidence" in result.update["errors"][0]
