"""DispatchNoticeBoard — one "not dispatched" comment per reason per ticket (#284)."""

from __future__ import annotations

from embry0.trackers import TrackerIssueRef
from embry0.trackers.notices import DispatchNoticeBoard

_REF = TrackerIssueRef(key="ENG-1", native_id="uuid-1")


class _Ledger:
    def __init__(self) -> None:
        self.claimed: set[tuple[str, str, str]] = set()

    async def claim(self, tracker: str, key: str, reason: str) -> bool:
        if (tracker, key, reason) in self.claimed:
            return False
        self.claimed.add((tracker, key, reason))
        return True

    async def release(self, tracker: str, key: str, reason: str) -> None:
        self.claimed.discard((tracker, key, reason))


class _Tracker:
    name = "linear"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.comments: list[tuple[TrackerIssueRef, str]] = []

    async def post_issue_comment(self, ref: TrackerIssueRef, body: str) -> bool:
        self.comments.append((ref, body))
        return self.ok


async def test_a_reason_is_commented_once_per_ticket() -> None:
    tracker = _Tracker()
    board = DispatchNoticeBoard(_Ledger(), tracker)

    assert await board.announce(_REF, "budget", "budget cap reached.") is True
    assert await board.announce(_REF, "budget", "budget cap reached.") is False
    assert await board.announce(_REF, "cap", "rate limit reached.") is True

    assert len(tracker.comments) == 2
    assert tracker.comments[0][0] is _REF
    assert tracker.comments[0][1] == "budget cap reached."  # the caller words the whole comment


async def test_an_undelivered_comment_releases_its_claim() -> None:
    ledger, tracker = _Ledger(), _Tracker(ok=False)
    board = DispatchNoticeBoard(ledger, tracker)

    assert await board.announce(_REF, "budget", "x") is False
    assert ledger.claimed == set()

    tracker.ok = True
    assert await board.announce(_REF, "budget", "x") is True


async def test_without_a_ledger_nothing_is_posted() -> None:
    tracker = _Tracker()

    assert await DispatchNoticeBoard(None, tracker).announce(_REF, "budget", "x") is False
    assert tracker.comments == []


async def test_a_broken_ledger_never_raises() -> None:
    class _Broken:
        async def claim(self, *a: str) -> bool:
            raise RuntimeError("db down")

    assert await DispatchNoticeBoard(_Broken(), _Tracker()).announce(_REF, "budget", "x") is False
