"""Dedupe keyed on the log signature, not on what the model called it (#290).

The audit measured `skipped_duplicate` firing **0 times in 1,339 ticks** while
the same `LazyInitializationException` was filed three separate times. The
fingerprint was a hash of the LLM's title, so a reworded re-detection was
always a new fingerprint.

These tests run a real `WatcherService` on real repositories, so they exercise
the whole path: cluster the window, remember the signatures, attribute the
proposal, look the signature up across every open case.
"""

from __future__ import annotations

import pytest

from tests.unit.services.healing.watcher_harness import (
    FakeLinear,
    build_watcher,
    proposal,
)

pytestmark = pytest.mark.requires_postgres

REPO = "owner/repo"

# The same fault, described three different ways — the exact shape that
# defeated the title hash.
WORDINGS = [
    proposal("LazyInitializationException on deal serialization"),
    proposal("Hibernate session closed while serializing a Deal"),
    proposal("Deal serialization fails: no Session"),
]


async def _tick(svc):
    return await svc.run_once()


async def test_a_reworded_redetection_lands_on_the_existing_case(db_with_migrations) -> None:
    """The headline of #290.

    Two ticks, identical log window, completely different proposal titles. The
    second must not open a second case.
    """
    svc, linear, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=WORDINGS[:2])

    first = await _tick(svc)
    assert first["action"] == "proposed", first

    second = await _tick(svc)
    assert second["action"] == "skipped_duplicate", (
        f"a reworded re-detection opened a new case instead of counting a recurrence: {second}"
    )

    rows = await db_with_migrations.fetch("SELECT id, signature_id, occurrence_count FROM healing_cases")
    assert len(rows) == 1, f"expected one case for one fault, got {len(rows)}"
    assert rows[0]["signature_id"], "the case was filed without a signature id"
    assert rows[0]["occurrence_count"] > 1, "the recurrence was not counted on the case"
    assert len(linear.created) == 1, "a second ticket was filed for the same signature"


async def test_the_case_records_the_signature_and_its_template(db_with_migrations) -> None:
    svc, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=WORDINGS[:1])
    await _tick(svc)

    row = await db_with_migrations.fetchrow("SELECT signature_id, signature, fingerprint FROM healing_cases")
    assert row["signature_id"] is not None
    assert row["signature"], "the signature template was not stored, so #312 has nothing to re-query"
    # The title hash is kept alongside, not replaced: rows written before
    # signatures existed are still found by it.
    assert row["fingerprint"] is not None


async def test_the_window_is_remembered_so_a_later_tick_can_compare(db_with_migrations) -> None:
    """#369: without the signatures repository wired, this table stays empty
    and every signature reads as new forever."""
    svc, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=WORDINGS[:1])
    await _tick(svc)

    rows = await db_with_migrations.fetch("SELECT signature_id, ticks_seen, occurrence_count FROM log_signatures")
    assert rows, "the tick clustered a window and recorded nothing"
    assert all(r["ticks_seen"] >= 1 for r in rows)


async def test_dedupe_is_not_bounded_by_the_proposal_window(db_with_migrations) -> None:
    """No 7-day expiry.

    The old check used `watcher_proposal_window_days`; a fault recurring after
    it was filed again. Set the window to zero days — under the old rule that
    expires every prior finding instantly — and the signature must still match.
    """
    svc, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=WORDINGS[:2], watcher_proposal_window_days=0)

    assert (await _tick(svc))["action"] == "proposed"
    second = await _tick(svc)
    assert second["action"] == "skipped_duplicate", (
        f"a zero-day proposal window let the same signature be filed twice: {second}"
    )
    assert len(await db_with_migrations.fetch("SELECT id FROM healing_cases")) == 1


async def test_dedupe_survives_a_restart(db_with_migrations) -> None:
    """The memory is the ledger, not the process."""
    linear = FakeLinear()
    svc, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=WORDINGS[:1], linear=linear)
    assert (await _tick(svc))["action"] == "proposed"

    # A new process talking to the same tracker and the same database.
    svc2, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=WORDINGS[1:2], linear=linear)
    assert (await _tick(svc2))["action"] == "skipped_duplicate"
    assert len(await db_with_migrations.fetch("SELECT id FROM healing_cases")) == 1


async def test_a_signature_returning_after_its_case_closed_links_to_it(db_with_migrations) -> None:
    """Regression of a closed case links to it, rather than losing the history."""
    svc, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=WORDINGS[:1])
    await _tick(svc)

    first = await db_with_migrations.fetchrow("SELECT id, signature_id FROM healing_cases")
    await db_with_migrations.execute(
        "UPDATE healing_cases SET stage = 'closed', closed_at = NOW() WHERE id = $1", first["id"]
    )

    svc2, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=WORDINGS[1:2])
    assert (await _tick(svc2))["action"] == "proposed", "a closed case must not suppress a regression"

    rows = await db_with_migrations.fetch("SELECT id, parent_case_id, signature_id FROM healing_cases ORDER BY id")
    assert len(rows) == 2, "the regression did not open a new case"
    regression = next(r for r in rows if r["id"] != first["id"])
    assert regression["parent_case_id"] == first["id"], (
        "the regression was filed with no link back to the case that closed"
    )
    assert regression["signature_id"] == first["signature_id"]


async def test_a_genuinely_different_signature_still_gets_its_own_case(db_with_migrations) -> None:
    """The guard against over-matching: dedupe must not swallow a new fault.

    One watcher, one tracker, two ticks over *different* log windows. Both
    findings are real and distinct, so both must be filed.
    """
    svc, linear, _ = build_watcher(
        db_with_migrations,
        repo=REPO,
        deliverables=[WORDINGS[0], proposal("Redis connection refused")],
        max_open=4,
    )

    assert (await _tick(svc))["action"] == "proposed"

    svc._fetch_log_entries = _entries(  # type: ignore[method-assign]
        ["[redis] connection refused 1", "[redis] connection refused 2", "[redis] connection refused 3"]
    )
    assert (await _tick(svc))["action"] == "proposed", "a second, unrelated fault was swallowed by the dedupe"

    rows = await db_with_migrations.fetch("SELECT signature_id FROM healing_cases")
    assert len(rows) == 2
    assert len({r["signature_id"] for r in rows}) == 2, "two different faults collapsed onto one signature"
    assert len(linear.created) == 2


def _entries(lines):
    from unittest.mock import AsyncMock

    from tests.unit.services.healing.watcher_harness import _as_entries

    return AsyncMock(return_value=_as_entries(lines))
