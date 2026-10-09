import pytest
from pydantic import ValidationError

from embry0.agents.triage_actions import (
    AskUser,
    RerunQA,
    RetryDeveloper,
    SetQADecision,
    clamp_advisory_strings,
)


def test_minimal_positive_decision():
    d = SetQADecision(needs_qa=True, reason="Frontend touched", acceptance_criteria=["home loads"])
    assert d.needs_qa is True


def test_negative_decision_with_empty_criteria():
    d = SetQADecision(needs_qa=False, reason="Docs only", acceptance_criteria=[])
    assert d.needs_qa is False


def test_positive_decision_can_have_empty_criteria():
    """Means: use qa.yaml.acceptance_criteria_template."""
    d = SetQADecision(needs_qa=True, reason="Backend route changed", acceptance_criteria=[])
    assert d.acceptance_criteria == []


def test_reason_required():
    with pytest.raises(ValidationError):
        SetQADecision(needs_qa=True, acceptance_criteria=[])


# ---------------------------------------------------------------------------
# RAV-1248: clamp_advisory_strings — over-long advisory strings truncate to
# the field max instead of failing validation downstream.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_cls", "field", "max_len"),
    [
        (RerunQA, "reason", 500),
        (RetryDeveloper, "prompt", 4000),
        (AskUser, "question", 2000),
        (SetQADecision, "reason", 500),
    ],
)
def test_clamp_truncates_over_long_field_to_max(model_cls, field, max_len):
    payload = {field: "x" * (max_len + 600)}
    clamped = clamp_advisory_strings(model_cls, payload)
    assert len(clamped[field]) == max_len
    assert clamped[field].endswith("…")


def test_clamped_over_long_reason_validates():
    """The exact RAV-1244 failure: a ~1,100-char rerun_qa reason must survive."""
    action = RerunQA.model_validate(clamp_advisory_strings(RerunQA, {"reason": "why " * 300}))
    assert len(action.reason) == 500


def test_clamp_leaves_in_bounds_payload_untouched():
    payload = {"reason": "boot timed out; prior attempt partially succeeded"}
    assert clamp_advisory_strings(RerunQA, payload) == payload


def test_clamp_ignores_non_string_and_unknown_fields():
    payload = {"reason": 42, "unknown": "y" * 5000}
    clamped = clamp_advisory_strings(RerunQA, payload)
    assert clamped == payload


def test_clamp_does_not_rescue_too_short_fields():
    """min_length violations are genuinely malformed and must still fail."""
    with pytest.raises(ValidationError):
        RetryDeveloper.model_validate(clamp_advisory_strings(RetryDeveloper, {"prompt": "hi"}))
