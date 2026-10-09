"""Durable per-ticket memory of the label-trigger path (#284).

Two small ledgers, both keyed on the tracker's own key rather than
``issues.id`` — an unmapped ticket never gets an embry0 row to hang state on:

* **notices** — "not dispatched because …" ticket comments. The trigger fires
  far more often than the reason changes (every ticket edit is a webhook,
  every reconciler sweep re-reads the ticket), so the comment must be posted
  once, not once per look. ``claim`` is the atomic "may I post this?": the
  primary key makes exactly one concurrent caller win.
* **failures** — dispatches that raised before a job row existed. They leave
  no job to count, so without this a ticket whose dispatch keeps failing is
  retried by every sweep forever. The count and timestamp drive the
  reconciler's backoff and attempt cap, and survive a restart.

A successful dispatch ``clear``s both: the ticket starts a new cycle, and a
later refusal or failure is news again.
"""

from __future__ import annotations

from typing import Any

from embry0.storage.database import DatabasePool


class DispatchLedgerRepository:
    """tracker_dispatch_notices + tracker_dispatch_failures."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    async def claim(self, tracker: str, tracker_key: str, reason: str) -> bool:
        """Record the notice. True only for the first caller of this triple."""
        row = await self._db.fetchrow(
            """
            INSERT INTO tracker_dispatch_notices (tracker, tracker_key, reason)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            RETURNING tracker_key
            """,
            tracker,
            tracker_key,
            reason,
        )
        return row is not None

    async def release(self, tracker: str, tracker_key: str, reason: str) -> None:
        """Forget a claim whose comment never reached the tracker."""
        await self._db.execute(
            "DELETE FROM tracker_dispatch_notices WHERE tracker = $1 AND tracker_key = $2 AND reason = $3",
            tracker,
            tracker_key,
            reason,
        )

    async def record_failure(self, tracker: str, tracker_key: str) -> int:
        """Count one dispatch that raised. Returns the ticket's running total."""
        count = await self._db.fetchval(
            """
            INSERT INTO tracker_dispatch_failures (tracker, tracker_key, failures, last_failed_at)
            VALUES ($1, $2, 1, NOW())
            ON CONFLICT (tracker, tracker_key) DO UPDATE SET
                failures = tracker_dispatch_failures.failures + 1,
                last_failed_at = NOW()
            RETURNING failures
            """,
            tracker,
            tracker_key,
        )
        return int(count or 0)

    async def failure_state(self, tracker: str, tracker_key: str) -> dict[str, Any] | None:
        """``{failures, last_failed_at}`` for the ticket, or None when it has none."""
        row = await self._db.fetchrow(
            "SELECT failures, last_failed_at FROM tracker_dispatch_failures WHERE tracker = $1 AND tracker_key = $2",
            tracker,
            tracker_key,
        )
        return dict(row) if row is not None else None

    async def clear(self, tracker: str, tracker_key: str) -> None:
        """A dispatch succeeded: forget the ticket's notices and failures."""
        await self._db.execute(
            "DELETE FROM tracker_dispatch_notices WHERE tracker = $1 AND tracker_key = $2", tracker, tracker_key
        )
        await self._db.execute(
            "DELETE FROM tracker_dispatch_failures WHERE tracker = $1 AND tracker_key = $2", tracker, tracker_key
        )
