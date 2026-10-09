"""The daily embry0 digest.

Replaces a notification per watcher analysis job — hourly, and delivered as raw
JSON because nothing downstream knew what to do with it — with one summary a
day of what embry0 actually did: jobs, PRs, spend, watcher ticks, healing cases
and confirm-healed verdicts.

Sent at most once per UTC day, whatever the restarts: the day is claimed in
``digest_deliveries`` before the webhook goes out, and given back if the
delivery fails so the next pass retries it. A process that starts after the
send hour sends the day's digest straight away rather than waiting a day.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import structlog

from embry0.orchestration import dispatch_actors

logger = structlog.get_logger(__name__)

#: How often the loop wakes to check whether today's digest is due. Short on
#: purpose: the claim makes extra wake-ups free, and a long sleep would let a
#: deploy near the send hour push the digest back by hours.
_CHECK_SECONDS = 600


def due_day(now: datetime, *, hour_utc: int) -> date | None:
    """The day whose digest is due at ``now``, or None before the send hour."""
    if now.tzinfo is None:
        raise ValueError("due_day needs a timezone-aware datetime")
    now = now.astimezone(UTC)
    return now.date() if now.time() >= time(hour_utc % 24) else None


def window_for(day: date, *, hour_utc: int) -> tuple[datetime, datetime]:
    """The 24 hours a day's digest covers: up to that day's send hour."""
    until = datetime.combine(day, time(hour_utc % 24), tzinfo=UTC)
    return until - timedelta(days=1), until


class DailyDigestService:
    def __init__(self, *, repo: Any, notifier: Any, config: Any) -> None:
        self._repo = repo
        self._notifier = notifier
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._config, "daily_digest_enabled", True)) and self._notifier is not None

    @property
    def hour_utc(self) -> int:
        return int(getattr(self._config, "daily_digest_hour_utc", 14)) % 24

    async def run_once(self, now: datetime | None = None) -> str:
        """Send today's digest if it is due and nobody has. Returns what happened."""
        now = now or datetime.now(UTC)
        day = due_day(now, hour_utc=self.hour_utc)
        if day is None:
            return "not_due"
        if not await self._repo.claim_day(day):
            return "already_sent"

        since, until = window_for(day, hour_utc=self.hour_utc)
        try:
            data = await self._repo.collect(since=since, until=until, watcher_actor=dispatch_actors.WATCHER)
            data["day"] = day.isoformat()
            delivered = await self._notifier.emit("digest.daily", data)
        except Exception:
            logger.warning("daily_digest_failed", day=day.isoformat(), exc_info=True)
            await self._repo.release_day(day)
            return "failed"
        if not delivered:
            # The notifier never raises; False means every retry failed. Give the
            # day back so the next check tries again instead of skipping a day.
            await self._repo.release_day(day)
            logger.warning("daily_digest_undelivered", day=day.isoformat())
            return "undelivered"
        await self._repo.mark_sent(day, data)
        logger.info("daily_digest_sent", day=day.isoformat(), jobs=data.get("jobs"))
        return "sent"

    async def start(self) -> None:  # pragma: no cover - exercised by the app
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.warning("daily_digest_loop_error", exc_info=True)
            await asyncio.sleep(_CHECK_SECONDS)
