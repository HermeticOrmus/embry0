"""The ask's summary and fixed-rule recommendation (pure; no I/O)."""

from __future__ import annotations

from typing import Any

from embry0.services.merge_recommendation import (
    SUMMARY_CHARS,
    ReviewFacts,
    parse_review,
    recommend,
    summarize,
)

REVIEW_JSON = """```json
{
    "decision": "approved",
    "validation": {
        "tests": {"status": "pass", "output": "986 tests"},
        "lint": {"status": "pass", "output": "clean"},
        "typecheck": {"status": "skipped", "output": ""}
    },
    "review_comments": [
        {"file": "a.py", "line": 1, "severity": "suggestion", "comment": "x"},
        {"file": "b.py", "line": 2, "severity": "important", "comment": "y"}
    ],
    "duplicates_open_pr": null,
    "addresses_logged_failure": {"verdict": "yes", "reason": "..."},
    "summary": "Focused, well-tested fix on the exact failing path. Full suite passes."
}
```"""

APPROVED = ReviewFacts(decision="approved", validation={"tests": "pass", "lint": "pass"}, summary="Good fix.")


def _facts(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "checks_state": "green",
        "checks_text": "all 8 checks passed",
        "checks_passed": 8,
        "mergeable": True,
        "denylisted": {},
        "files_complete": True,
        "additions": 40,
        "deletions": 10,
        "changed_files": 3,
        "verification": {"present": True, "blocking": False, "for_this_head": True, "human_merge_reasons": []},
        "review": APPROVED,
    }
    return {**base, **overrides}


# -- parsing the reviewer ---------------------------------------------------


def test_the_reviewer_json_is_read_from_its_fence() -> None:
    review = parse_review(REVIEW_JSON)
    assert review is not None
    assert review.decision == "approved"
    assert review.validation == {"tests": "pass", "lint": "pass", "typecheck": "skipped"}
    assert (review.critical, review.important) == (0, 1)
    assert review.addresses_failure == "yes"
    assert review.duplicate_pr is None
    assert review.summary.startswith("Focused, well-tested fix")


def test_text_without_reviewer_json_is_no_review() -> None:
    assert parse_review(None) is None
    assert parse_review("TITLE: something\nBODY: prose") is None
    assert parse_review('{"not": "a review"}') is None


def test_a_duplicate_pr_is_read() -> None:
    review = parse_review('{"decision": "approved", "duplicates_open_pr": {"number": 12, "reason": "same"}}')
    assert review is not None and review.duplicate_pr == 12


# -- the summary ------------------------------------------------------------


def test_the_reviewer_summary_comes_first() -> None:
    summary, source = summarize(parse_review(REVIEW_JSON), "A PR body paragraph that is long enough.", "t")
    assert source == "review"
    assert summary.startswith("Focused")


def test_the_pr_body_is_the_fallback_without_its_boilerplate() -> None:
    body = "## Summary\n\nOpened by the embry0 orchestrator: the developer reported no PR.\n\nFixes the lane rounding bug in quotes by using half-even."
    summary, source = summarize(None, body, "fix(quotes): rounding")
    assert source == "pr_body"
    assert summary == "Fixes the lane rounding bug in quotes by using half-even."


def test_an_issue_reference_alone_is_boilerplate() -> None:
    assert summarize(None, "Fixes #12", "fix: y") == ("fix: y", "title")
    assert summarize(None, "Closes acme/backend#700", "fix: y") == ("fix: y", "title")


def test_the_title_is_the_last_resort() -> None:
    assert summarize(None, "Opened by the embry0 orchestrator: x", "fix: y") == ("fix: y", "title")
    assert summarize(None, None, "fix: y") == ("fix: y", "title")


def test_a_long_summary_is_clipped() -> None:
    long = ReviewFacts(decision="approved", summary="word " * 400)
    summary, _ = summarize(long, None, "t")
    assert len(summary) <= SUMMARY_CHARS + 1


# -- the verdict --------------------------------------------------------------


def test_skipped_tests_are_a_reason_to_look_twice() -> None:
    review = ReviewFacts(decision="approved", validation={"tests": "skipped"}, summary="static only")
    rec = recommend(**_facts(review=review))
    assert rec.verdict == "review_first", "skipped tests must not read as a clean approval"
    assert any("did not run tests" in reason for reason in rec.reasons)


def test_green_ci_approved_review_and_nothing_sensitive_recommends_merge() -> None:
    rec = recommend(**_facts())
    assert rec.verdict == "merge"
    assert rec.reasons == []
    assert "CI green (8 checks)" in rec.positives
    assert "reviewer approved" in rec.positives


def test_red_ci_says_dont_merge() -> None:
    rec = recommend(**_facts(checks_state="red", checks_text="checks failed: build (failure)"))
    assert rec.verdict == "dont_merge"
    assert any("CI failed" in r for r in rec.reasons)


def test_changes_requested_says_dont_merge() -> None:
    rec = recommend(**_facts(review=ReviewFacts(decision="changes_requested")))
    assert rec.verdict == "dont_merge"
    assert "the reviewer requested changes" in rec.reasons


def test_a_failing_reviewer_validation_or_critical_comment_says_dont_merge() -> None:
    assert recommend(**_facts(review=ReviewFacts(decision="approved", validation={"tests": "fail"}))).verdict == (
        "dont_merge"
    )
    assert recommend(**_facts(review=ReviewFacts(decision="approved", critical=1))).verdict == "dont_merge"


def test_conflicts_or_a_blocking_gate_say_dont_merge() -> None:
    assert recommend(**_facts(mergeable=False)).verdict == "dont_merge"
    blocking = {"present": True, "blocking": True, "for_this_head": True, "human_merge_reasons": []}
    assert recommend(**_facts(verification=blocking)).verdict == "dont_merge"


def test_a_blocking_gate_for_another_commit_does_not_block() -> None:
    old = {"present": True, "blocking": True, "for_this_head": False, "human_merge_reasons": []}
    assert recommend(**_facts(verification=old)).verdict == "merge"


def test_sensitive_paths_say_review_first() -> None:
    rec = recommend(**_facts(denylisted={"migrations": {"count": 1, "paths": ["db/migration/V1.sql"]}}))
    assert rec.verdict == "review_first"
    assert "touches migrations" in rec.reasons


def test_ci_that_did_not_settle_says_review_first() -> None:
    rec = recommend(**_facts(checks_state="pending", checks_text="still not finished after 45 min"))
    assert rec.verdict == "review_first"


def test_no_embry0_review_says_review_first() -> None:
    rec = recommend(**_facts(review=None))
    assert rec.verdict == "review_first"
    assert any("no embry0 review" in r for r in rec.reasons)


def test_important_comments_doubt_or_overlap_say_review_first() -> None:
    for review in (
        ReviewFacts(decision="approved", important=2),
        ReviewFacts(decision="approved", addresses_failure="no"),
        ReviewFacts(decision="approved", duplicate_pr=12),
    ):
        assert recommend(**_facts(review=review)).verdict == "review_first", review


def test_a_large_diff_says_review_first() -> None:
    assert recommend(**_facts(additions=900)).verdict == "review_first"
    assert recommend(**_facts(changed_files=40)).verdict == "review_first"


def test_an_unconfigured_gate_is_a_note_not_a_downgrade() -> None:
    """Nearly every PR today has no deterministic gate; counting that would
    make every verdict "review first" and the verdict meaningless."""
    unconfigured = {
        "present": True,
        "blocking": False,
        "for_this_head": False,
        "human_merge_reasons": ["gate_not_configured"],
    }
    rec = recommend(**_facts(verification=unconfigured))
    assert rec.verdict == "merge"
    assert any("no deterministic test gate" in n for n in rec.notes)


def test_a_gate_error_or_untested_frontend_says_review_first() -> None:
    for reason in ("gate_error", "gate_base_red_unverified", "no_branch_stack"):
        verification = {"present": True, "blocking": False, "for_this_head": True, "human_merge_reasons": [reason]}
        assert recommend(**_facts(verification=verification)).verdict == "review_first", reason


def test_dont_merge_wins_and_keeps_the_cautions() -> None:
    rec = recommend(**_facts(checks_state="red", checks_text="x", denylisted={"auth": {"count": 1, "paths": []}}))
    assert rec.verdict == "dont_merge"
    assert "touches auth" in rec.reasons
