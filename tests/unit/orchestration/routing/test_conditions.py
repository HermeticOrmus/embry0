"""Tests for routing condition functions."""

import json

import pytest

from embry0.orchestration.routing.conditions import (
    check_budget,
    check_review_decision,
    check_triage_action,
    parse_review_decision,
)
from embry0.orchestration.routing.conditions import (
    tests_block_reason as approval_block_reason,  # aliased: pytest would collect a test_* name
)

# --- check_triage_action ---


def test_triage_action_proceed():
    # action lives in triage_decision (D4: pipeline_config is now the flat dict)
    state = {"triage_decision": {"action": "proceed"}}
    assert check_triage_action(state) == "proceed"


def test_triage_action_split():
    state = {"triage_decision": {"action": "split"}}
    assert check_triage_action(state) == "split"


def test_triage_action_needs_info_routes_to_proceed():
    """needs_info is handled by interrupt, not routing — should map to proceed."""
    state = {"triage_decision": {"action": "needs_info"}}
    assert check_triage_action(state) == "proceed"


def test_triage_action_empty_state():
    state = {}
    assert check_triage_action(state) == "proceed"


# --- check_review_decision ---


def test_review_decision_no_outputs():
    state = {"agent_outputs": []}
    assert check_review_decision(state) == "changes_requested"


def test_review_decision_no_review_outputs():
    state = {"agent_outputs": [{"agent_type": "developer", "output": "done"}]}
    assert check_review_decision(state) == "changes_requested"


def test_review_decision_approved_json():
    review_data = json.dumps({"decision": "approved", "summary": "LGTM"})
    state = {"agent_outputs": [{"agent_type": "review", "output": review_data}]}
    assert check_review_decision(state) == "approved"


def test_review_decision_changes_requested_under_max_retries():
    review_data = json.dumps({"decision": "changes_requested", "summary": "Needs fixes"})
    state = {
        "agent_outputs": [{"agent_type": "review", "output": review_data}],
        "retry_count": 1,
        "pipeline_config": {"max_feedback_loops": 3},  # flat dict (D4)
    }
    assert check_review_decision(state) == "changes_requested"


def test_review_decision_changes_requested_at_max_retries():
    review_data = json.dumps({"decision": "changes_requested", "summary": "Still broken"})
    state = {
        "agent_outputs": [{"agent_type": "review", "output": review_data}],
        "retry_count": 3,
        "pipeline_config": {"max_feedback_loops": 3},  # flat dict (D4)
    }
    assert check_review_decision(state) == "max_retries"


def test_review_decision_changes_requested_over_max_retries():
    review_data = json.dumps({"decision": "changes_requested"})
    state = {
        "agent_outputs": [{"agent_type": "review", "output": review_data}],
        "retry_count": 5,
        "pipeline_config": {"max_feedback_loops": 3},  # flat dict (D4)
    }
    assert check_review_decision(state) == "max_retries"


def test_review_decision_approved_but_docs_need_update():
    review_data = json.dumps(
        {
            "decision": "approved",
            "docs_review": {"needs_update": True, "files": ["README.md"]},
        }
    )
    state = {
        "agent_outputs": [{"agent_type": "review", "output": review_data}],
        "retry_count": 0,
        "pipeline_config": {"max_feedback_loops": 3},  # flat dict (D4)
    }
    assert check_review_decision(state) == "changes_requested"


def test_review_decision_approved_docs_no_update():
    review_data = json.dumps(
        {
            "decision": "approved",
            "docs_review": {"needs_update": False},
        }
    )
    state = {"agent_outputs": [{"agent_type": "review", "output": review_data}]}
    assert check_review_decision(state) == "approved"


# --- check_budget ---


def test_budget_within():
    # pipeline_config is now the flat dict (D4: no nested "pipeline_config" wrapper)
    state = {"total_cost_usd": 5.0, "pipeline_config": {"budget_usd": 10.0}}
    assert check_budget(state) == "within_budget"


def test_budget_over():
    state = {"total_cost_usd": 15.0, "pipeline_config": {"budget_usd": 10.0}}
    assert check_budget(state) == "over_budget"


def test_budget_at_boundary():
    """Exactly at budget should be within (not strictly over)."""
    state = {"total_cost_usd": 10.0, "pipeline_config": {"budget_usd": 10.0}}
    assert check_budget(state) == "within_budget"


def test_review_decision_errored_agent_fails_closed():
    """An errored review agent must NOT auto-approve — fail closed to max_retries
    (regression: errored review was silently treated as 'approved')."""
    state = {"agent_outputs": [{"agent_type": "review", "is_error": True, "output": ""}]}
    assert check_review_decision(state) == "max_retries"


# --- #356: an unreadable verdict must never route to `approved` ---------------
#
# Characterisation of the exact ten shapes proven to auto-approve before the fix.
# Each one completed the job, set current_stage=review_passed, closed the issue,
# fired the job.completed webhook and could open a PR. On the historical corpus
# this converts up to 18 silently-approved jobs into human pauses.

UNREADABLE_REVIEW_OUTPUTS: list[tuple[str, object]] = [
    ("empty", ""),
    ("whitespace_only", "   \n\t  "),
    ("unparseable_prose", "I looked at the diff and it seems fine overall."),
    # The worst shape: the reviewer says REJECT in prose and used to be approved.
    ("prose_saying_reject", "VERDICT: REJECT. Do not merge."),
    ("fence_without_decision_key", '```json\n{"summary": "looks fine"}\n```'),
    ("unknown_decision_string", json.dumps({"decision": "rejected"})),
    ("json_null", "null"),
    ("truncated_json", '{"decision": "appro'),
    ("non_object_json", "123"),
    ("non_string_output", None),
]


@pytest.mark.parametrize("shape,output", UNREADABLE_REVIEW_OUTPUTS, ids=[s for s, _ in UNREADABLE_REVIEW_OUTPUTS])
def test_unreadable_review_never_approves(shape: str, output: object) -> None:
    """No unreadable shape may yield a verdict, and none may route to approved."""
    assert parse_review_decision(output) is None, f"{shape} produced a verdict"
    state = {"agent_outputs": [{"agent_type": "review", "output": output}]}
    assert check_review_decision(state) == "max_retries", f"{shape} did not fail closed"


def test_missing_output_key_fails_closed():
    """A review output dict with no `output` key at all (#356 case 8)."""
    state = {"agent_outputs": [{"agent_type": "review"}]}
    assert check_review_decision(state) == "max_retries"


def test_unreadable_review_is_not_charged_a_developer_round():
    """max_retries, not changes_requested: an unreadable review carries no
    findings, so a developer round would be spent on nothing."""
    state = {
        "agent_outputs": [{"agent_type": "review", "output": "no verdict here"}],
        "retry_count": 0,
        "pipeline_config": {"max_feedback_loops": 5},
    }
    assert check_review_decision(state) == "max_retries"


def test_keyword_fallback_still_downgrades_unparseable_prose():
    """The keyword fallback survives — it can only ever downgrade, never approve."""
    state = {"agent_outputs": [{"agent_type": "review", "output": "I want changes_requested here"}]}
    assert parse_review_decision("I want changes_requested here") == "changes_requested"
    assert check_review_decision(state) == "changes_requested"


def test_valid_approved_mentioning_the_phrase_stays_approved():
    """A parsed `approved` is not downgraded because its prose mentions the
    phrase — the keyword fallback is consulted only when no verdict was read."""
    output = json.dumps({"decision": "approved", "summary": "No changes_requested this round"})
    assert parse_review_decision(output) == "approved"
    assert check_review_decision({"agent_outputs": [{"agent_type": "review", "output": output}]}) == "approved"


def test_fenced_approved_block_still_parses():
    """The markdown-fence path keeps working for a well-formed verdict."""
    output = '```json\n{"decision": "approved", "summary": "LGTM"}\n```'
    assert parse_review_decision(output) == "approved"


def test_fenced_docs_needs_update_downgrades():
    output = '```json\n{"decision": "approved", "docs_review": {"needs_update": true}}\n```'
    assert parse_review_decision(output) == "changes_requested"


# --- a bare JSON verdict after narration -------------------------------------
#
# grok-4.7 narrates each step as its own message and writes the verdict as bare
# JSON, with no fence. The executors join every message, so the output is prose
# followed by the object. 3 of grok's first 4 reviews after #479 (2026-10-05)
# carried a valid "approved" there and were paused as unreadable.

GROK_SHAPED_REVIEW = (
    "I'll review the fix against the logged btree overflow, starting with the diff.\n\n"
    "The diff touches `{normalized_name}` handling and the ingest path; I'll trace both.\n\n"
    '{\n  "decision": "approved",\n  "validation": {"tests": {"status": "pass", "reason": null}},\n'
    '  "docs_review": {"needs_update": false},\n  "summary": "Fixes the failing write path."\n}'
)


def test_a_bare_json_verdict_after_narration_is_read():
    assert parse_review_decision(GROK_SHAPED_REVIEW) == "approved", "the verdict after the narration was not read"
    state = {"agent_outputs": [{"agent_type": "review", "output": GROK_SHAPED_REVIEW}]}
    assert check_review_decision(state) == "approved"


def test_the_last_verdict_object_is_the_one_read():
    """Narration can quote an example verdict; the reviewer's own is the last one."""
    output = (
        'The prompt asks for {"decision": "changes_requested"} when tests fail. They pass.\n\n'
        '{"decision": "approved", "summary": "tests pass"}'
    )
    assert parse_review_decision(output) == "approved", "an earlier quoted object must not outrank the verdict"


def test_a_bare_verdict_still_downgrades_on_stale_docs():
    output = 'Checked the docs.\n\n{"decision": "approved", "docs_review": {"needs_update": true}}'
    assert parse_review_decision(output) == "changes_requested"


def test_a_bare_approval_with_a_failed_check_is_blocked():
    """The approval guards read the same payload: unread, a failing check slipped past them."""
    output = 'Ran the suite.\n\n{"decision": "approved", "validation": {"tests": {"status": "fail"}}}'
    assert approval_block_reason(output) == "failed_checks", "a bare verdict's failed check must block the approval"


def _review_state(output: str) -> dict:
    return {
        "agent_outputs": [{"agent_type": "review", "output": output}],
        "retry_count": 0,
        "pipeline_config": {"max_feedback_loops": 3},
    }


def test_approved_with_failing_tests_requests_changes():
    """A reviewer that says approved while a check failed did not approve."""
    output = json.dumps(
        {"decision": "approved", "validation": {"tests": {"status": "fail", "output": "AssertionError"}}}
    )
    assert check_review_decision(_review_state(output)) == "changes_requested", "a failing test must not approve"


def test_missing_compiler_does_not_approve_or_retry():
    """No JDK (the RAV-2414 shape) is an environment failure. Sending the
    developer back into the same sandbox cannot install a compiler."""
    output = json.dumps(
        {
            "decision": "approved",
            "validation": {
                "tests": {"status": "skipped", "output": "Compiling isn't possible (no JVM in sandbox)."},
            },
            "summary": "Static review only.",
        }
    )
    assert check_review_decision(_review_state(output)) == "max_retries", (
        "a missing compiler must not approve or burn a developer retry"
    )


def test_explicit_no_toolchain_reason_pauses():
    output = json.dumps(
        {
            "decision": "approved",
            "validation": {"tests": {"status": "skipped", "reason": "no_toolchain", "output": "java: not found"}},
        }
    )
    assert check_review_decision(_review_state(output)) == "max_retries", (
        "reason no_toolchain must pause instead of approving"
    )


def test_skipped_tests_without_a_reason_request_changes():
    output = json.dumps({"decision": "approved", "validation": {"tests": {"status": "skipped", "output": "not run"}}})
    assert check_review_decision(_review_state(output)) == "changes_requested", (
        "skipped tests with no reason must not approve"
    )


def test_tests_skipped_because_a_service_is_missing_can_approve():
    """The sandbox has no database. A runner that started and skipped for
    that reason is still an approval — the prompt allows it."""
    output = json.dumps(
        {
            "decision": "approved",
            "validation": {
                "tests": {"status": "skipped", "reason": "needs_service", "output": "postgres is not in this sandbox"},
            },
        }
    )
    assert check_review_decision(_review_state(output)) == "approved"
