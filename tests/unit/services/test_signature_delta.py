"""What changed in a window, per signature (#290 SH-P3.2, #291 SH-P3.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from embry0.services.log_signatures import LogCluster
from embry0.services.signature_delta import (
    Change,
    classify_window,
)
from embry0.storage.repositories.log_signatures import SignatureRow

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _cluster(sig: str, count: int, template: str | None = None) -> LogCluster:
    return LogCluster(signature_id=sig, template=template or f"template for {sig}", count=count)


def _row(sig: str, *, last_window_count: int, last_seen_at: datetime, occurrences: int = 100) -> SignatureRow:
    return SignatureRow(
        signature_id=sig,
        repo="owner/repo",
        template=f"template for {sig}",
        first_seen_at=last_seen_at - timedelta(days=3),
        last_seen_at=last_seen_at,
        occurrence_count=occurrences,
        ticks_seen=10,
        last_window_count=last_window_count,
        services=["svc"],
        sample_lines=["a line"],
    )


def test_a_signature_never_recorded_is_new() -> None:
    delta = classify_window([_cluster("aaa", 4)], {}, window_start=NOW)
    assert [c.change for c in delta.changes] == [Change.NEW]
    assert delta.has_notable is True


def test_a_signature_at_its_usual_volume_is_steady() -> None:
    prior = {"aaa": _row("aaa", last_window_count=40, last_seen_at=NOW - timedelta(minutes=30))}
    delta = classify_window([_cluster("aaa", 44)], prior, window_start=NOW)
    assert [c.change for c in delta.changes] == [Change.STEADY]
    assert delta.has_notable is False


def test_a_signature_far_louder_than_last_window_is_spiking() -> None:
    prior = {"aaa": _row("aaa", last_window_count=10, last_seen_at=NOW - timedelta(minutes=30))}
    delta = classify_window([_cluster("aaa", 90)], prior, window_start=NOW)
    change = delta.changes[0]
    assert change.change is Change.SPIKING
    assert change.previous_count == 10
    assert change.count == 90


def test_a_small_multiple_is_not_a_spike() -> None:
    """1 -> 3 is a 3x ratio and means nothing; the absolute floor is what stops it."""
    prior = {"aaa": _row("aaa", last_window_count=1, last_seen_at=NOW - timedelta(minutes=30))}
    delta = classify_window([_cluster("aaa", 3)], prior, window_start=NOW)
    assert delta.changes[0].change is Change.STEADY
    assert delta.has_notable is False


def test_a_signature_returning_after_a_long_silence_is_regressed() -> None:
    prior = {"aaa": _row("aaa", last_window_count=50, last_seen_at=NOW - timedelta(hours=30))}
    delta = classify_window([_cluster("aaa", 12)], prior, window_start=NOW)
    change = delta.changes[0]
    assert change.change is Change.REGRESSED
    assert change.silent_seconds == pytest.approx(30 * 3600)


def test_regression_outranks_spiking_for_the_same_signature() -> None:
    """Came back after a day AND louder: 'it is back' is the more useful label."""
    prior = {"aaa": _row("aaa", last_window_count=2, last_seen_at=NOW - timedelta(hours=30))}
    delta = classify_window([_cluster("aaa", 99)], prior, window_start=NOW)
    assert delta.changes[0].change is Change.REGRESSED


def test_primary_prefers_a_notable_signature_over_a_louder_steady_one() -> None:
    """The whole point of attribution: a loud, known fault must not mask a new one."""
    clusters = [_cluster("loud", 500), _cluster("fresh", 7)]
    prior = {"loud": _row("loud", last_window_count=480, last_seen_at=NOW - timedelta(minutes=20))}
    delta = classify_window(clusters, prior, window_start=NOW)
    assert delta.primary is not None
    assert delta.primary.signature_id == "fresh"
    assert delta.primary.change is Change.NEW


def test_primary_falls_back_to_the_loudest_when_nothing_is_notable() -> None:
    clusters = [_cluster("loud", 500), _cluster("quiet", 7)]
    prior = {
        "loud": _row("loud", last_window_count=480, last_seen_at=NOW - timedelta(minutes=20)),
        "quiet": _row("quiet", last_window_count=6, last_seen_at=NOW - timedelta(minutes=20)),
    }
    delta = classify_window(clusters, prior, window_start=NOW)
    assert delta.has_notable is False
    assert delta.primary is not None
    assert delta.primary.signature_id == "loud"


def test_primary_is_stable_for_the_same_window() -> None:
    """An attribution that moved between ticks would key a case on a new
    signature each time, which is the bug #290 exists to fix."""
    clusters = [_cluster("bbb", 9), _cluster("aaa", 9)]
    first = classify_window(clusters, {}, window_start=NOW).primary
    second = classify_window(list(clusters), {}, window_start=NOW).primary
    assert first is not None and second is not None
    assert first.signature_id == second.signature_id


def test_an_empty_window_has_no_primary() -> None:
    delta = classify_window([], {}, window_start=NOW)
    assert delta.primary is None
    assert delta.has_notable is False
    assert delta.counts() == {"new": 0, "spiking": 0, "regressed": 0, "steady": 0}


def test_counts_tally_every_change_class() -> None:
    clusters = [_cluster("a", 60), _cluster("b", 50), _cluster("c", 40), _cluster("d", 30)]
    prior = {
        "b": _row("b", last_window_count=2, last_seen_at=NOW - timedelta(minutes=10)),  # spiking
        "c": _row("c", last_window_count=200, last_seen_at=NOW - timedelta(hours=40)),  # regressed
        "d": _row("d", last_window_count=28, last_seen_at=NOW - timedelta(minutes=10)),  # steady
    }
    counts = classify_window(clusters, prior, window_start=NOW).counts()
    assert counts == {"new": 1, "spiking": 1, "regressed": 1, "steady": 1}


def test_without_a_window_start_a_regression_cannot_be_claimed() -> None:
    """No clock here: with nothing to measure silence against, the honest
    answer is 'not regressed', never an invented duration."""
    prior = {"aaa": _row("aaa", last_window_count=50, last_seen_at=NOW - timedelta(hours=30))}
    delta = classify_window([_cluster("aaa", 12)], prior)
    assert delta.changes[0].change is Change.STEADY
    assert delta.changes[0].silent_seconds is None
