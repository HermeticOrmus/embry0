"""Pure rules of merge sync: reading a forge PR object, ordering facts."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from embry0.services.merge_sync.facts import (
    DeliveryState,
    fact_from_github,
    parse_forge_time,
    resolve_delivery,
)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _pr(**over):
    base = {
        "html_url": "https://github.com/acme/widgets/pull/7",
        "number": 7,
        "state": "open",
        "merged_at": None,
        "closed_at": None,
        "created_at": "2026-08-01T10:00:00Z",
        "updated_at": "2026-08-01T12:00:00Z",
        "merge_commit_sha": "cafe",
        "head": {"ref": "embry0/unknown-fix", "sha": "abc123"},
        "base": {"repo": {"full_name": "acme/widgets"}},
    }
    return {**base, **over}


def test_open_pr_is_dated_by_its_last_update():
    fact = fact_from_github(_pr())
    assert fact is not None
    assert (fact.state, fact.at) == (DeliveryState.PR_OPEN, T0)
    assert (fact.repo, fact.number, fact.head_ref, fact.head_sha) == ("acme/widgets", 7, "embry0/unknown-fix", "abc123")
    # GitHub fills merge_commit_sha with a TEST merge on open PRs: not a merge.
    assert fact.merge_sha is None


def test_merged_pr_is_dated_by_the_merge_not_by_later_comments():
    fact = fact_from_github(
        _pr(
            state="closed",
            merged_at="2026-08-02T09:00:00Z",
            closed_at="2026-08-02T09:00:00Z",
            updated_at="2026-09-01T00:00:00Z",
        )
    )
    assert fact is not None
    assert fact.state is DeliveryState.MERGED
    assert fact.at == datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
    assert fact.merge_sha == "cafe"


def test_webhook_merged_flag_without_merged_at_still_reads_as_merged():
    fact = fact_from_github(_pr(state="closed", merged=True, closed_at="2026-08-02T09:00:00Z"))
    assert fact is not None and fact.state is DeliveryState.MERGED
    assert fact.at == datetime(2026, 8, 2, 9, 0, tzinfo=UTC)


def test_closed_unmerged_is_dated_by_the_close():
    fact = fact_from_github(_pr(state="closed", closed_at="2026-08-03T09:00:00Z", updated_at="2026-09-01T00:00:00Z"))
    assert fact is not None
    assert (fact.state, fact.at) == (DeliveryState.CLOSED, datetime(2026, 8, 3, 9, 0, tzinfo=UTC))


def test_renamed_repo_is_normalised_in_url_and_slug(monkeypatch):
    monkeypatch.setattr(
        "embry0.storage.repo_renames.REPO_RENAMES",
        {"acme/old": "acme/new"},
    )
    fact = fact_from_github(
        _pr(
            html_url="https://github.com/acme/old/pull/418",
            base={"repo": {"full_name": "acme/old"}},
        )
    )
    assert fact is not None
    assert fact.url == "https://github.com/acme/new/pull/418"
    assert fact.repo == "acme/new"


def test_a_pr_object_without_a_url_is_not_a_fact():
    assert fact_from_github({"number": 1}) is None


@pytest.mark.parametrize("raw", [None, "", "yesterday", 5, "2026-08-01T10:00:00"])
def test_unusable_forge_times_are_none_never_naive(raw):
    assert parse_forge_time(raw) is None


def test_merged_is_final():
    later = T0 + timedelta(days=1)
    assert resolve_delivery("merged", T0, DeliveryState.CLOSED, later) is None
    assert resolve_delivery("merged", T0, DeliveryState.PR_OPEN, later) is None
    assert resolve_delivery("merged", T0, DeliveryState.MERGED, later) is None


def test_a_merge_replaces_anything_even_when_dated_earlier():
    change = resolve_delivery("closed", T0, DeliveryState.MERGED, T0 - timedelta(hours=1))
    assert change is not None and change.state is DeliveryState.MERGED and change.state_changed


def test_a_migrated_merge_without_a_date_gets_one():
    change = resolve_delivery("merged", None, DeliveryState.MERGED, T0)
    assert change is not None and change.at == T0 and not change.state_changed


def test_an_older_fact_never_replaces_a_newer_one():
    assert resolve_delivery("pr_open", T0, DeliveryState.CLOSED, T0 - timedelta(minutes=1)) is None
    assert resolve_delivery("closed", T0, DeliveryState.PR_OPEN, T0 - timedelta(minutes=1)) is None


def test_an_undated_stored_state_yields_to_any_fact():
    change = resolve_delivery("pr_open", None, DeliveryState.CLOSED, T0)
    assert change is not None and change.state is DeliveryState.CLOSED


def test_an_undated_fact_yields_to_a_dated_state():
    assert resolve_delivery("pr_open", T0, DeliveryState.CLOSED, None) is None


def test_a_sweep_does_not_rewrite_an_open_pr_for_every_comment():
    later = T0 + timedelta(hours=1)
    assert resolve_delivery("pr_open", T0, DeliveryState.PR_OPEN, later, advance_same_state=False) is None
    moved = resolve_delivery("pr_open", T0, DeliveryState.PR_OPEN, later)
    assert moved is not None and moved.at == later and not moved.state_changed


def test_identical_redelivery_changes_nothing():
    assert resolve_delivery("closed", T0, DeliveryState.CLOSED, T0) is None


def _timeline(rng: random.Random) -> list[tuple[DeliveryState, datetime]]:
    """A legal PR life: opened, any number of close/reopen rounds, an ending."""
    when = T0
    events = [(DeliveryState.PR_OPEN, when)]
    for _ in range(rng.randint(0, 3)):
        when += timedelta(minutes=rng.randint(1, 600))
        events.append((DeliveryState.CLOSED, when))
        when += timedelta(minutes=rng.randint(1, 600))
        events.append((DeliveryState.PR_OPEN, when))
    ending = rng.choice(["open", "closed", "merged"])
    if ending != "open":
        when += timedelta(minutes=rng.randint(1, 600))
        events.append((DeliveryState.MERGED if ending == "merged" else DeliveryState.CLOSED, when))
    return events


@pytest.mark.parametrize("seed", range(40))
def test_any_delivery_order_with_redeliveries_ends_at_the_truth(seed):
    rng = random.Random(seed)
    truth = _timeline(rng)
    deliveries = truth + [rng.choice(truth) for _ in range(rng.randint(0, 6))]
    rng.shuffle(deliveries)

    state, at = "none", None
    for fact_state, fact_at in deliveries:
        change = resolve_delivery(state, at, fact_state, fact_at)
        if change is not None:
            state, at = change.state, change.at
    assert (state, at) == truth[-1]
