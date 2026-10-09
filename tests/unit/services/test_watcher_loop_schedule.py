"""WatcherService.start(): ticks are scheduled from what is recorded (#335).

The decision itself is covered exhaustively in test_loop_schedule.py; these
tests pin how the loop feeds it and what it does around a tick.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog

from embry0.services.watcher import WatcherService
from tests.unit.services.test_watcher import _LOKI_PARSE_ERROR, _config, _loki, _service

T0 = datetime(2026, 9, 20, 5, 22, tzinfo=UTC)
_OK = httpx.Response(200, json={"data": {"result": []}})


class _Loop:
    """Drives start() on a fake clock: sleeping advances it, nothing else does."""

    def __init__(self, svc: WatcherService, *, iterations: int, tick_seconds: float = 0.0) -> None:
        self.svc = svc
        self.now = T0
        self.sleeps: list[float] = []
        self.events: list[str] = []

        async def sleep(seconds: float) -> None:
            if len(self.sleeps) >= iterations:
                raise asyncio.CancelledError
            self.sleeps.append(seconds)
            self.now += timedelta(seconds=seconds)

        async def run_once() -> dict:
            self.events.append("tick")
            self.now += timedelta(seconds=tick_seconds)
            # what the real run_once leaves behind: a run row stamped at the end
            svc._runs.last_tick_at.return_value = self.now
            return {"action": "no_issue"}

        async def record_attempt(at: datetime, repo: str = "") -> None:
            self.events.append("attempt")
            svc._runs.last_tick_attempt_at.return_value = at

        svc._sleep = sleep
        svc._now = lambda: self.now
        svc.run_once = run_once
        svc._runs.record_tick_attempt = AsyncMock(side_effect=record_attempt)
        svc._http = _loki(lambda r: _OK)

    async def run(self) -> _Loop:
        with pytest.raises(asyncio.CancelledError):
            await self.svc.start()
        return self


def _svc(**kwargs):
    return _service(config=_config(watcher_deploy_logql="", watcher_startup_delay_seconds=90), **kwargs)


@pytest.mark.asyncio
async def test_overdue_tick_runs_after_the_startup_delay_then_hourly():
    """The 2026-09-20 case: last tick 04:14, fifth deploy at 05:22."""
    loop = await _Loop(_svc(last_tick_at=T0 - timedelta(minutes=68)), iterations=3, tick_seconds=600).run()
    assert loop.sleeps == [90, 3600, 3600]
    assert loop.events == ["attempt", "tick"] * 3


@pytest.mark.asyncio
async def test_tick_that_is_not_due_waits_out_its_interval_not_a_fresh_one():
    loop = await _Loop(_svc(last_tick_at=T0 - timedelta(minutes=8)), iterations=2).run()
    assert loop.sleeps == [52 * 60, 3600]


@pytest.mark.asyncio
async def test_never_ticked_is_overdue():
    loop = await _Loop(_svc(), iterations=1).run()
    assert loop.sleeps == [90]


def test_the_shipped_startup_delay_is_90_seconds():
    from embry0.config import Embry0Config

    assert Embry0Config.model_fields["watcher_startup_delay_seconds"].default == 90


@pytest.mark.asyncio
async def test_a_tick_killed_mid_analysis_is_not_rerun_by_the_next_process():
    """Crash loop: the previous process marked its attempt 10 minutes ago and
    died inside the analysis job — no run row. A job per restart is the
    failure this guards against."""
    svc = _svc(last_tick_at=T0 - timedelta(hours=5), last_tick_attempt_at=T0 - timedelta(minutes=10))
    loop = await _Loop(svc, iterations=1).run()
    assert loop.sleeps == [50 * 60]


@pytest.mark.asyncio
async def test_the_attempt_is_marked_before_the_tick_with_the_loop_clock():
    svc = _svc()
    loop = await _Loop(svc, iterations=1).run()
    assert loop.events == ["attempt", "tick"]
    svc._runs.record_tick_attempt.assert_awaited_once_with(T0 + timedelta(seconds=90), repo=svc._repo)


@pytest.mark.asyncio
async def test_a_tick_that_cannot_leave_its_mark_does_not_run():
    svc = _svc()
    loop = _Loop(svc, iterations=2)
    svc._runs.record_tick_attempt = AsyncMock(side_effect=RuntimeError("db away"))
    with structlog.testing.capture_logs() as logs:
        await loop.run()
    assert loop.events == []
    assert loop.sleeps == [90, 3600]  # and it does not spin on the startup delay
    assert [e["event"] for e in logs if e["log_level"] == "error"] == ["watcher_tick_attempt_unrecorded"] * 2


@pytest.mark.asyncio
async def test_unreadable_anchors_fall_back_to_the_full_interval():
    """Not knowing when the last tick was must never be what starts a job."""
    svc = _svc()
    loop = _Loop(svc, iterations=1)
    svc._runs.last_tick_at = AsyncMock(side_effect=RuntimeError("db away"))
    with structlog.testing.capture_logs() as logs:
        await loop.run()
    assert loop.sleeps == [3600]
    assert "watcher_schedule_unreadable" in {e["event"] for e in logs}


@pytest.mark.asyncio
async def test_a_blocked_watcher_marks_no_attempt_and_waits_whole_intervals():
    """tick_blocked is still honoured: an invalid query costs no tick, leaves
    no mark (so the fix + recreate ticks after the startup delay, not an hour
    later), and the loop does not re-probe Loki every 90 s."""
    svc = _svc()
    loop = _Loop(svc, iterations=3)
    svc._http = _loki(lambda r: httpx.Response(400, text=_LOKI_PARSE_ERROR))
    await loop.run()
    assert svc.tick_blocked
    assert loop.events == []
    assert loop.sleeps == [90, 3600, 3600]


@pytest.mark.asyncio
async def test_three_restarts_inside_one_interval_keep_the_original_due_time():
    """Acceptance: a restart never pushes two ticks further apart than
    interval + startup delay. Each boot is a fresh service over the same store."""
    last_tick = T0
    fire_times = []
    for minutes_after in (5, 31, 59.5):
        svc = _svc(last_tick_at=last_tick)
        loop = _Loop(svc, iterations=1)
        loop.now = T0 + timedelta(minutes=minutes_after)
        await loop.run()
        fire_times.append(loop.now)
    assert fire_times[:2] == [T0 + timedelta(hours=1)] * 2
    assert fire_times[2] == T0 + timedelta(minutes=59.5, seconds=90)
    assert all(fire - last_tick <= timedelta(seconds=3600 + 90) for fire in fire_times)


@pytest.mark.asyncio
async def test_a_persistent_unrecordable_mark_degrades_readiness():
    """Fail-closed on cost means no tick, which is silence — the reviewer's
    harness ran this for 24 h and readiness stayed green. It must not."""
    svc = _svc()
    loop = _Loop(svc, iterations=4)
    svc._runs.record_tick_attempt = AsyncMock(side_effect=RuntimeError("db away"))
    with structlog.testing.capture_logs() as logs:
        await loop.run()
    assert loop.events == []  # still no tick — the cost guard is unchanged
    assert svc.health == "tick_unrecordable"
    levels = [e["log_level"] for e in logs if e["event"] == "watcher_tick_attempt_unrecorded"]
    assert levels == ["error", "error", "critical", "critical"]


@pytest.mark.asyncio
async def test_two_failed_marks_are_a_blip_not_a_fault():
    svc = _svc()
    loop = _Loop(svc, iterations=2)
    svc._runs.record_tick_attempt = AsyncMock(side_effect=RuntimeError("db away"))
    await loop.run()
    assert svc.health == "ok"


@pytest.mark.asyncio
async def test_a_recorded_mark_clears_the_fault():
    svc = _svc()
    loop = _Loop(svc, iterations=4)
    calls = {"n": 0}
    recorded = svc._runs.record_tick_attempt

    async def flaky(at, repo=""):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("db away")
        await recorded(at)

    svc._runs.record_tick_attempt = AsyncMock(side_effect=flaky)
    await loop.run()
    assert svc.health == "ok"
    assert loop.events == ["attempt", "tick"]


@pytest.mark.asyncio
async def test_the_loop_survives_anything_an_iteration_throws():
    """start()'s task is fire-and-forget: an escape here ends the watcher
    with no log line at all."""
    svc = _svc()
    loop = _Loop(svc, iterations=3)
    boom = AsyncMock(side_effect=RuntimeError("health probe exploded"))
    svc._iterate = boom
    with structlog.testing.capture_logs() as logs:
        await loop.run()
    assert boom.await_count == 3  # it kept going
    assert loop.sleeps == [90, 3600, 3600]
    assert [e["event"] for e in logs if e["log_level"] == "error"] == ["watcher_loop_iteration_failed"] * 3


@pytest.mark.asyncio
async def test_the_schedule_reads_this_stacks_ticks_not_every_stacks():
    """#378: one watcher per stack. The last-tick anchor must be the stack's
    own, or a stack that has never ticked inherits its neighbour's cadence
    (seen on the first embry0 self-stack start: `last_tick_at` equal to the
    acme stack's) and a stalled stack keeps looking current."""
    svc = _service(
        config=_config(watcher_deploy_logql="", watcher_startup_delay_seconds=90, watcher_repo="acme/backend"),
        last_tick_at=T0 - timedelta(minutes=68),
    )
    await _Loop(svc, iterations=1).run()
    calls = svc._runs.last_tick_at.await_args_list
    assert calls, "the schedule never read the last-tick anchor"
    assert all(c.kwargs.get("repo") == "acme/backend" for c in calls), (
        f"last_tick_at was read without the stack's repo: {calls}"
    )
