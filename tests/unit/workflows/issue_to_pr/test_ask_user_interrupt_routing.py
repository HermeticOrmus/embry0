"""RAV-1271 — ask_user_interrupt resume routing + zero-question guard.

The interrupt node must:
  - surface the pending questions and, on resume, route back to the ASKING
    node (triage / developer / review) with the answers on ``user_answers``
    (the old static edge sent every resume to the developer, so a triage
    QA-escalation answer never reached triage);
  - fail loudly to END with ``ERR_EMPTY_INTERRUPT`` when it is reached with
    zero questions (previously it raised an interrupt nobody could see and
    the job ended corrupt: status=completed, finished_at NULL).
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.graph import END
from langgraph.types import Command

from embry0.safety.error_codes import ErrorCode
from embry0.workflows.issue_to_pr.nodes import ask_user_interrupt


@pytest.fixture(autouse=True)
def _stub_stream_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace get_stream_writer so the node runs outside a LangGraph runtime."""
    import embry0.workflows.issue_to_pr.nodes as nodes_module

    monkeypatch.setattr(nodes_module, "get_stream_writer", lambda: lambda _e: None)


def _patch_interrupt(monkeypatch: pytest.MonkeyPatch, answers: Any) -> list[dict[str, Any]]:
    """Patch the interrupt seam to 'resume' immediately with ``answers``.

    Returns a list capturing the payloads the node raised with.
    """
    import embry0.workflows.issue_to_pr.nodes as nodes_module

    raised: list[dict[str, Any]] = []

    def _fake_interrupt(payload: dict[str, Any]) -> Any:
        raised.append(payload)
        return answers

    monkeypatch.setattr(nodes_module, "interrupt", _fake_interrupt)
    return raised


def _question(asking_node: str) -> dict[str, Any]:
    return {"question": "which way?", "asking_node": asking_node, "importance": "blocking"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asking_node", "expected_goto"),
    [("triage", "triage"), ("developer", "developer"), ("review", "review")],
)
async def test_resume_reenters_the_asking_node(
    monkeypatch: pytest.MonkeyPatch, asking_node: str, expected_goto: str
) -> None:
    raised = _patch_interrupt(monkeypatch, answers=[{"question": "which way?", "answer": "left"}])
    state = {"job_id": "j1", "pending_agent_questions": [_question(asking_node)]}

    result = await ask_user_interrupt(state, config=None)

    assert isinstance(result, Command)
    assert result.goto == expected_goto
    assert result.update["user_answers"] == [{"question": "which way?", "answer": "left"}]
    assert result.update["pending_agent_questions"] == []
    # The interrupt payload still carries the asker + questions for the executor.
    assert raised and raised[0]["asking_node"] == asking_node
    assert raised[0]["reason"] == "agent_needs_info"


@pytest.mark.asyncio
async def test_unknown_asking_node_falls_back_to_developer(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_interrupt(monkeypatch, answers="ok")
    state = {"job_id": "j1", "pending_agent_questions": [_question("mystery_node")]}

    result = await ask_user_interrupt(state, config=None)

    assert isinstance(result, Command)
    assert result.goto == "developer"


@pytest.mark.asyncio
async def test_zero_questions_fails_loudly_instead_of_pausing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero pending questions ⇒ no interrupt raised at all; the job routes to
    END with ERR_EMPTY_INTERRUPT (never the silent corrupt completion)."""
    raised = _patch_interrupt(monkeypatch, answers=None)
    state = {"job_id": "j1", "pending_agent_questions": [], "current_stage": "qa_ask_user"}

    result = await ask_user_interrupt(state, config=None)

    assert isinstance(result, Command)
    assert result.goto == END
    assert result.update["current_stage"] == "failed"
    assert result.update["error_code"] == ErrorCode.EMPTY_INTERRUPT.value
    assert raised == [], "no interrupt may be raised when there is nothing to ask"
