"""Signature memory keeps recording when the draft cap is full (#369 follow-up).

Measured on 2026-09-21, after #369 wired the repository: every tick for 8+
hours ended `skipped_analysis_cap`, which returned before clustering, and
`log_signatures` stayed at 0 rows. A memory that stops whenever the cap is
full lets a still-firing fault look silent, which confirm-healed (#312) reads
as healed.

Moving the recording earlier creates the opposite hazard, pinned here too: a
tick that does not advance the query window re-reads the same lines, and
record_window ADDS counts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from tests.unit.services.healing.watcher_harness import LINES, _as_entries, build_watcher, proposal

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

REPO = "owner/repo"


def _full_cap_watcher(db):
    # max_open=0 makes the cap full from the first tick; a zero full-cap
    # analysis budget makes every tick end skipped_analysis_cap — exactly the
    # production state measured on 2026-09-21.
    svc, _, jobs = build_watcher(
        db,
        repo=REPO,
        deliverables=[proposal("never analysed")] * 5,
        max_open=0,
        watcher_max_full_cap_analyses_per_day=0,
    )
    return svc, jobs


async def _sig_rows(db):
    return await db.fetch("SELECT signature_id, occurrence_count, ticks_seen, last_seen_at FROM log_signatures")


async def test_a_full_cap_tick_still_records_its_signatures(db_with_migrations) -> None:
    svc, jobs = _full_cap_watcher(db_with_migrations)
    out = await svc.run_once()
    assert out["action"] == "skipped_analysis_cap", out
    assert not jobs.rows, "the fixture paid for an analysis; it must model a full cap"
    assert await _sig_rows(db_with_migrations), "a full-cap tick clustered nothing into log_signatures"


async def test_re_reading_the_same_window_does_not_inflate_counts(db_with_migrations) -> None:
    """A skipped tick does not advance the window, so the next tick sees the
    very same lines. They were counted once and must stay counted once."""
    svc, _ = _full_cap_watcher(db_with_migrations)
    await svc.run_once()
    await svc.run_once()
    await svc.run_once()
    rows = await _sig_rows(db_with_migrations)
    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == len(LINES), (
        f"3 ticks over one unchanged window counted {rows[0]['occurrence_count']} occurrences of {len(LINES)} lines"
    )


async def test_lines_newer_than_last_seen_are_counted(db_with_migrations) -> None:
    svc, _ = _full_cap_watcher(db_with_migrations)
    await svc.run_once()
    before = (await _sig_rows(db_with_migrations))[0]

    later = _as_entries(LINES, base=datetime(2026, 9, 20, 13, 0, tzinfo=UTC))
    svc._fetch_log_entries = AsyncMock(return_value=_as_entries(LINES) + later)  # type: ignore[method-assign]
    await svc.run_once()
    after = (await _sig_rows(db_with_migrations))[0]

    assert after["occurrence_count"] == before["occurrence_count"] + len(LINES), (
        "the new lines were not counted, or the old ones were counted again"
    )
    assert after["last_seen_at"] > before["last_seen_at"], "last_seen_at did not move forward — #312 would read silence"
