"""#304 — qa_failure_action validation, independent of the triage node."""

from __future__ import annotations

import pytest

from embry0.agents.triage_actions import AskUser, RerunQA, RetryDeveloper
from embry0.workflows.issue_to_pr.nodes._qa_failure_action import (
    QAFailureActionInvalid,
    build_repair_prompt,
    parse_qa_failure_action,
)


def test_parses_each_kind_into_its_model() -> None:
    retry = parse_qa_failure_action(
        {"qa_failure_action": {"kind": "retry_developer", "prompt": "Fix the widget fetch.", "focus_files": ["a.py"]}}
    )
    rerun = parse_qa_failure_action({"qa_failure_action": {"kind": "rerun_qa", "reason": "flaky boot"}})
    ask = parse_qa_failure_action({"qa_failure_action": {"kind": "ask_user", "question": "Were the creds rotated?"}})

    assert isinstance(retry, RetryDeveloper) and retry.focus_files == ["a.py"]
    assert isinstance(rerun, RerunQA)
    assert isinstance(ask, AskUser)


def test_over_long_advisory_string_is_clamped_not_rejected() -> None:
    action = parse_qa_failure_action({"qa_failure_action": {"kind": "rerun_qa", "reason": "x" * 835}})

    assert isinstance(action, RerunQA)
    assert len(action.reason) == 500


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        ({}, "missing"),
        ({"qa_failure_action": "rerun_qa"}, "missing"),
        ({"qa_failure_action": {"kind": "halt_universe"}}, "unknown_kind"),
        ({"qa_failure_action": {"kind": "retry_developer", "prompt": "hi"}}, "invalid_payload"),
        (
            {"qa_failure_action": {"kind": "ask_user", "question": "Were they rotated?", "focus_files": ["a.py"]}},
            "invalid_payload",
        ),
    ],
)
def test_invalid_actions_name_their_reason(decision: dict, reason: str) -> None:
    with pytest.raises(QAFailureActionInvalid) as excinfo:
        parse_qa_failure_action(decision)

    assert excinfo.value.reason == reason
    assert excinfo.value.detail


def test_repair_prompt_carries_the_error_and_the_contract() -> None:
    with pytest.raises(QAFailureActionInvalid) as excinfo:
        parse_qa_failure_action({"qa_failure_action": {"kind": "ask_user", "question": "Were they rotated?", "x": 1}})

    prompt = build_repair_prompt(excinfo.value)

    assert excinfo.value.detail in prompt
    for kind in ("retry_developer", "rerun_qa", "ask_user"):
        assert kind in prompt


# ---- empty sibling-kind fields are dropped, deterministically ---------------


def test_empty_field_of_a_sibling_kind_is_dropped_with_a_warning() -> None:
    """job-d05a769e7865 verbatim: ask_user + retry_developer's focus_files=[]."""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        action = parse_qa_failure_action(
            {"qa_failure_action": {"kind": "ask_user", "question": "Were the creds rotated?", "focus_files": []}}
        )

    assert isinstance(action, AskUser)
    dropped = [entry for entry in logs if entry["event"] == "triage_qa_failure_action_empty_sibling_fields_dropped"]
    assert dropped and dropped[0]["log_level"] == "warning"
    assert dropped[0]["fields"] == ["focus_files"] and dropped[0]["kind"] == "ask_user"


@pytest.mark.parametrize("empty", [None, "", [], {}])
def test_every_empty_shape_of_a_sibling_field_is_dropped(empty: object) -> None:
    action = parse_qa_failure_action({"qa_failure_action": {"kind": "rerun_qa", "reason": "flaky", "prompt": empty}})

    assert isinstance(action, RerunQA)


def test_non_empty_sibling_field_is_still_invalid() -> None:
    """A rerun_qa that ALSO names files to fix is confused about what it is
    asking for — that needs the agent's attention, not a silent drop."""
    with pytest.raises(QAFailureActionInvalid) as excinfo:
        parse_qa_failure_action({"qa_failure_action": {"kind": "rerun_qa", "reason": "flaky", "focus_files": ["a.py"]}})

    assert excinfo.value.reason == "invalid_payload"
    assert "focus_files" in excinfo.value.detail


def test_empty_unknown_key_is_still_invalid() -> None:
    with pytest.raises(QAFailureActionInvalid) as excinfo:
        parse_qa_failure_action({"qa_failure_action": {"kind": "rerun_qa", "reason": "flaky", "notes": []}})

    assert "notes" in excinfo.value.detail


def test_empty_value_of_the_kinds_own_field_is_not_dropped() -> None:
    with pytest.raises(QAFailureActionInvalid) as excinfo:
        parse_qa_failure_action({"qa_failure_action": {"kind": "rerun_qa", "reason": ""}})

    assert "reason" in excinfo.value.detail


# ---- repair answer + prompt -------------------------------------------------


def test_repair_prompt_shows_the_rejected_action() -> None:
    """On the non-resumable path the model has no memory of its own answer."""
    rejected = {"kind": "rerun_qa", "reason": "flaky", "focus_files": ["a.py"]}
    with pytest.raises(QAFailureActionInvalid) as excinfo:
        parse_qa_failure_action({"qa_failure_action": rejected})

    prompt = build_repair_prompt(excinfo.value)

    assert '"focus_files": ["a.py"]' in prompt


def test_repair_prompt_says_so_when_the_action_was_absent() -> None:
    with pytest.raises(QAFailureActionInvalid) as excinfo:
        parse_qa_failure_action({})

    assert "did not include" in build_repair_prompt(excinfo.value)


@pytest.mark.parametrize(
    "answer",
    [
        '{"kind": "rerun_qa", "reason": "flaky"}',
        '```json\n{"kind": "rerun_qa", "reason": "flaky"}\n```',
        'Fixed:\n```json\n{"qa_failure_action": {"kind": "rerun_qa", "reason": "flaky"}}\n```',
        '{"action": "proceed", "confidence": 0.9, "qa_failure_action": {"kind": "rerun_qa", "reason": "flaky"}}',
    ],
)
def test_repaired_action_is_found_bare_wrapped_or_in_a_full_decision(answer: str) -> None:
    from embry0.workflows.issue_to_pr.nodes._qa_failure_action import extract_repaired_action

    assert extract_repaired_action(answer) == {"kind": "rerun_qa", "reason": "flaky"}


@pytest.mark.parametrize("answer", ["", "I cannot decide.", "[1, 2]", '{"action": "proceed"}'])
def test_repaired_action_is_none_when_the_answer_has_no_action(answer: str) -> None:
    from embry0.workflows.issue_to_pr.nodes._qa_failure_action import extract_repaired_action

    assert extract_repaired_action(answer) is None
