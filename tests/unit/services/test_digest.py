"""The daily digest: when it is due, and that it is sent exactly once."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from embry0.services.digest import DailyDigestService, due_day, window_for


def test_nothing_is_due_before_the_send_hour() -> None:
    assert due_day(datetime(2026, 9, 21, 13, 59, tzinfo=UTC), hour_utc=14) is None


def test_the_day_is_due_from_the_send_hour_on() -> None:
    """A process that starts after the hour sends at once, not tomorrow."""
    assert due_day(datetime(2026, 9, 21, 14, 0, tzinfo=UTC), hour_utc=14) == date(2026, 9, 21)
    assert due_day(datetime(2026, 9, 21, 23, 30, tzinfo=UTC), hour_utc=14) == date(2026, 9, 21)


def test_a_naive_clock_is_refused() -> None:
    with pytest.raises(ValueError):
        due_day(datetime(2026, 9, 21, 15, 0), hour_utc=14)


def test_the_window_is_the_24_hours_up_to_the_send_hour() -> None:
    since, until = window_for(date(2026, 9, 21), hour_utc=14)
    assert until == datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
    assert since == datetime(2026, 9, 20, 14, 0, tzinfo=UTC)


class _Repo:
    def __init__(self, *, claim: bool = True, collect_raises: bool = False) -> None:
        self.claim = claim
        self.collect_raises = collect_raises
        self.sent: list[Any] = []
        self.released: list[date] = []

    async def claim_day(self, day):
        return self.claim

    async def collect(self, **_):
        if self.collect_raises:
            raise RuntimeError("db gone")
        return {"jobs": {"completed": 2}}

    async def mark_sent(self, day, payload):
        self.sent.append((day, payload))

    async def release_day(self, day):
        self.released.append(day)


class _Notifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event, data):
        self.events.append((event, data))
        return self.ok


AFTER = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)


def _svc(repo, notifier):
    return DailyDigestService(repo=repo, notifier=notifier, config=SimpleNamespace(daily_digest_hour_utc=14))


@pytest.mark.asyncio
async def test_a_due_digest_is_sent_and_recorded() -> None:
    repo, notifier = _Repo(), _Notifier()
    assert await _svc(repo, notifier).run_once(AFTER) == "sent"
    assert notifier.events[0][0] == "digest.daily"
    assert notifier.events[0][1]["day"] == "2026-09-21"
    assert repo.sent and not repo.released


@pytest.mark.asyncio
async def test_a_day_someone_else_claimed_is_not_sent_again() -> None:
    repo, notifier = _Repo(claim=False), _Notifier()
    assert await _svc(repo, notifier).run_once(AFTER) == "already_sent"
    assert not notifier.events


@pytest.mark.asyncio
async def test_an_undelivered_digest_gives_the_day_back() -> None:
    """Otherwise a receiver outage at the send hour silently skips the day."""
    repo, notifier = _Repo(), _Notifier(ok=False)
    assert await _svc(repo, notifier).run_once(AFTER) == "undelivered"
    assert repo.released == [date(2026, 9, 21)]
    assert not repo.sent


@pytest.mark.asyncio
async def test_a_collection_failure_gives_the_day_back() -> None:
    repo, notifier = _Repo(collect_raises=True), _Notifier()
    assert await _svc(repo, notifier).run_once(AFTER) == "failed"
    assert repo.released == [date(2026, 9, 21)]
    assert not notifier.events


@pytest.mark.asyncio
async def test_not_due_touches_nothing() -> None:
    repo, notifier = _Repo(), _Notifier()
    assert await _svc(repo, notifier).run_once(datetime(2026, 9, 21, 9, 0, tzinfo=UTC)) == "not_due"
    assert not notifier.events and not repo.sent


def test_the_digest_is_on_by_default() -> None:
    from embry0.config import Embry0Config

    c = Embry0Config(_env_file=None, database_url="postgresql://unused")
    assert c.daily_digest_enabled is True
    assert 0 <= c.daily_digest_hour_utc <= 23
