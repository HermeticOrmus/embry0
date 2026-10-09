"""Tell the ticket why it was not dispatched — once per reason (#284).

Before this, every non-dispatch outcome of a label trigger was a log line on
the orchestrator: the human who applied the label saw nothing happen and had
no way to learn that the project carried no ``repo:`` stamp, or that the
budget cap was reached. The board posts that reason on the ticket through the
tracker's ordinary ``post_issue_comment`` path, and consults the
``tracker_dispatch_notices`` ledger first so a reason is stated once per
ticket however many deliveries and sweeps observe it.
"""

from __future__ import annotations

from typing import Any, Protocol

import structlog

from embry0.trackers.models import TrackerIssueRef

logger = structlog.get_logger(__name__)


class _Commenter(Protocol):
    @property
    def name(self) -> str: ...

    async def post_issue_comment(self, ref: TrackerIssueRef, body: str) -> bool: ...


class DispatchNoticeBoard:
    """Posts at most one comment per (ticket, reason).

    ``dispatch_ledger`` is a ``DispatchLedgerRepository`` (or None: with no
    ledger there is no way to honour "once", so nothing is posted — the reason
    still reaches the log).
    """

    def __init__(self, dispatch_ledger: Any, commenter: _Commenter) -> None:
        self._repo = dispatch_ledger
        self._commenter = commenter

    async def announce(self, ref: TrackerIssueRef, reason: str, detail: str) -> bool:
        """Comment ``detail`` — the full text, worded by the caller — under ``reason``.

        True when a comment was posted.

        Best-effort and never raises: a broken ledger or tracker must not turn
        a declined dispatch into a failed webhook delivery.
        """
        if self._repo is None:
            return False
        tracker = self._commenter.name
        try:
            if not await self._repo.claim(tracker, ref.key, reason):
                return False
            posted = await self._commenter.post_issue_comment(ref, detail)
            if not posted:
                # The human never saw it; let the next observation try again.
                await self._repo.release(tracker, ref.key, reason)
            return posted
        except Exception:
            logger.warning("dispatch_notice_failed", tracker=tracker, key=ref.key, reason=reason, exc_info=True)
            return False
