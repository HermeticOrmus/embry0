"""Confirm-healed: the window arithmetic and the verdict (#312, SH-P5.4).

The loop ended at "merged". Nothing checked that the error stopped, so RAV-419
closed LazyInitializationException while it was still running at 22-82 lines a
day. Of 26-33 merged fixes, zero had a recorded outcome.

The dangerous wrong answer is `healed` on a rare signature that simply had no
time to fire — it closes the ticket. Most of this file is about that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from embry0.services.healing.confirm import (
    EXPECTED_OCCURRENCES,
    MAX_WINDOW,
    MIN_WINDOW,
    Readiness,
    judge,
    observation_window,
    plan_verification,
)
from embry0.services.healing.lifecycle import Verdict

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


# ---- the window is scaled to the signature's own rate ----------------------


def test_a_loud_signature_is_judged_quickly() -> None:
    """500/h expects 3 occurrences in well under a minute, so the floor binds."""
    assert observation_window(500.0) == MIN_WINDOW


def test_a_rare_signature_gets_a_long_window() -> None:
    assert observation_window(0.05) > timedelta(hours=12)


def test_the_window_is_never_shorter_than_the_floor() -> None:
    """The minutes right after a restart are the least representative the
    service will have."""
    assert observation_window(10_000.0) == MIN_WINDOW


def test_the_window_is_never_longer_than_the_cap() -> None:
    assert observation_window(0.000001) == MAX_WINDOW


def test_an_unknown_rate_gets_the_longest_window() -> None:
    """Not knowing how often it fired means the only safe assumption is rarely."""
    assert observation_window(None) == MAX_WINDOW
    assert observation_window(0) == MAX_WINDOW


@pytest.mark.parametrize("rate", [0.5, 2.0, 40.0])
def test_the_window_expects_the_configured_number_of_occurrences(rate: float) -> None:
    window = observation_window(rate)
    if MIN_WINDOW < window < MAX_WINDOW:
        assert rate * window.total_seconds() / 3600.0 == pytest.approx(EXPECTED_OCCURRENCES)


# ---- readiness -------------------------------------------------------------


def test_a_case_judged_before_its_window_elapses_is_too_early() -> None:
    plan = plan_verification(deployed_at=NOW - timedelta(minutes=2), now=NOW, prior_rate_per_hour=100.0)
    assert plan.readiness is Readiness.TOO_EARLY
    assert not plan.ready


def test_a_case_past_its_window_is_ready() -> None:
    plan = plan_verification(deployed_at=NOW - timedelta(hours=2), now=NOW, prior_rate_per_hour=100.0)
    assert plan.readiness is Readiness.READY


def test_a_signature_too_rare_to_settle_is_unjudgeable() -> None:
    """One every three weeks: even the maximum window expects less than one
    occurrence, so silence proves nothing and waiting will not help."""
    plan = plan_verification(deployed_at=NOW - timedelta(days=30), now=NOW, prior_rate_per_hour=0.002)
    assert plan.readiness is Readiness.UNJUDGEABLE


def test_a_deploy_in_the_future_does_not_produce_negative_elapsed() -> None:
    plan = plan_verification(deployed_at=NOW + timedelta(hours=1), now=NOW, prior_rate_per_hour=100.0)
    assert plan.elapsed == timedelta(0)
    assert plan.readiness is Readiness.TOO_EARLY


# ---- the verdict -----------------------------------------------------------


def _ready(rate: float | None = 50.0, elapsed: timedelta = timedelta(hours=1)):
    return plan_verification(deployed_at=NOW - elapsed, now=NOW, prior_rate_per_hour=rate)


def test_a_signature_not_seen_since_the_deploy_is_healed() -> None:
    j = judge(
        signature_id="sig1",
        repo="owner/repo",
        seen_since_deploy=False,
        occurrences_after_at_least=0,
        prior_rate_per_hour=50.0,
        plan=_ready(),
        last_seen_at=NOW - timedelta(hours=3),
        deployed_at=NOW - timedelta(hours=1),
    )
    assert j.verdict is Verdict.HEALED
    assert j.evidence["after_at_least"] == 0
    assert j.evidence["expected_if_unfixed"] > 0, "the counterfactual is the whole argument"


def test_any_reappearance_at_all_is_not_healed() -> None:
    """A fault signature has no tolerance band — one is enough."""
    j = judge(
        signature_id="sig1",
        repo="owner/repo",
        seen_since_deploy=True,
        occurrences_after_at_least=1,
        prior_rate_per_hour=50.0,
        plan=_ready(),
        last_seen_at=NOW - timedelta(minutes=5),
        deployed_at=NOW - timedelta(hours=1),
    )
    assert j.verdict is Verdict.NOT_HEALED
    assert j.evidence["seen_since_deploy"] is True


def test_a_case_with_no_signature_is_inconclusive_not_healed() -> None:
    """The dangerous default. Nothing to count must never read as 'it stopped'."""
    j = judge(
        signature_id=None,
        repo="owner/repo",
        seen_since_deploy=False,
        occurrences_after_at_least=0,
        prior_rate_per_hour=None,
        plan=_ready(rate=None),
    )
    assert j.verdict is Verdict.INCONCLUSIVE


def test_a_signature_too_rare_to_judge_is_inconclusive_not_healed() -> None:
    """Silence from something that fires monthly is not evidence of a fix."""
    plan = plan_verification(deployed_at=NOW - timedelta(days=30), now=NOW, prior_rate_per_hour=0.002)
    j = judge(
        signature_id="sig1",
        repo="owner/repo",
        seen_since_deploy=False,
        occurrences_after_at_least=0,
        prior_rate_per_hour=0.002,
        plan=plan,
    )
    assert j.verdict is Verdict.INCONCLUSIVE, "a rare signature's silence was mistaken for a fix"


def test_the_evidence_is_counts_and_never_log_lines() -> None:
    """The ledger refuses evidence over 16 KiB and the API serves it."""
    import json

    j = judge(
        signature_id="sig1",
        repo="owner/repo",
        seen_since_deploy=True,
        occurrences_after_at_least=9,
        prior_rate_per_hour=50.0,
        plan=_ready(),
        last_seen_at=NOW,
        deployed_at=NOW - timedelta(hours=1),
    )
    blob = json.dumps(j.evidence, default=str)
    assert len(blob) < 2000
    assert "before_rate_per_hour" in j.evidence and "window_s" in j.evidence
    assert j.evidence["source"] == "log_signatures"


def test_the_lower_bound_is_labelled_as_one() -> None:
    """log_signatures keeps a lifetime total and no per-range history, so the
    exact count since the deploy is not recoverable. The field name has to say
    so rather than present a bound as a measurement."""
    j = judge(
        signature_id="sig1",
        repo="owner/repo",
        seen_since_deploy=True,
        occurrences_after_at_least=4,
        prior_rate_per_hour=50.0,
        plan=_ready(),
    )
    assert "after_at_least" in j.evidence
    assert "after" not in j.evidence, "an exact-sounding field would overstate what is known"


# ---- #657: a signature the evidence never quoted proves nothing ------------


@pytest.mark.parametrize("seen_since_deploy", [False, True])
def test_a_fallback_signature_is_inconclusive_whatever_it_did(seen_since_deploy: bool) -> None:
    """The window's loudest line stopping (or not) says nothing about this case's error."""
    j = judge(
        signature_id="sig1",
        repo="owner/repo",
        seen_since_deploy=seen_since_deploy,
        occurrences_after_at_least=3 if seen_since_deploy else 0,
        prior_rate_per_hour=50.0,
        plan=_ready(),
        signature_source="fallback",
    )
    assert j.verdict is Verdict.INCONCLUSIVE, (
        f"a fallback-anchored case was judged {j.verdict.value} on a line its evidence never quoted"
    )
    assert j.evidence["signature_source"] == "fallback"
    assert "reason" in j.evidence
    # The counts stay, for a person who wants to read them anyway.
    assert j.evidence["seen_since_deploy"] is seen_since_deploy


@pytest.mark.parametrize(("source", "expected"), [("evidence", Verdict.HEALED), (None, Verdict.HEALED)])
def test_an_evidence_or_unrecorded_source_is_judged_as_before(source: str | None, expected: Verdict) -> None:
    j = judge(
        signature_id="sig1",
        repo="owner/repo",
        seen_since_deploy=False,
        occurrences_after_at_least=0,
        prior_rate_per_hour=50.0,
        plan=_ready(),
        signature_source=source,
    )
    assert j.verdict is expected
    assert j.evidence["signature_source"] == source
