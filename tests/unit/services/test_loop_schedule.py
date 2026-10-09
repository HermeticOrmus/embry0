"""The interval-loop scheduling decision (#335), driven by a fake clock.

Every case is a statement about one pure function, so restart sequences are
simulated by calling it again with a later ``now`` — no process, no sleep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from embry0.services.loop_schedule import seconds_until_next_run

T0 = datetime(2026, 9, 20, 4, 14, tzinfo=UTC)
INTERVAL = 3600
DELAY = 90


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _sleep(now: datetime, *, last_run=None, last_attempt=None, min_delay: float = DELAY) -> float:
    return seconds_until_next_run(
        now=now,
        interval_seconds=INTERVAL,
        min_delay_seconds=min_delay,
        last_run_at=last_run,
        last_attempt_at=last_attempt,
    )


def test_never_ran_is_overdue_and_waits_only_the_startup_delay():
    assert _sleep(_at(0)) == DELAY


def test_overdue_tick_waits_only_the_startup_delay():
    assert _sleep(_at(125), last_run=T0) == DELAY


def test_tick_not_yet_due_waits_out_the_rest_of_the_interval():
    assert _sleep(_at(20), last_run=T0) == 40 * 60


def test_tick_due_inside_the_startup_delay_still_waits_the_delay():
    # due in 30 s, but a deploy must not compete with an analysis job
    assert _sleep(T0 + timedelta(seconds=INTERVAL - 30), last_run=T0) == DELAY


def test_steady_state_has_no_floor():
    """After the first iteration the caller passes min_delay 0: a run recorded
    a moment ago means one full interval, a due one means now."""
    assert _sleep(_at(0), last_run=T0, min_delay=0) == INTERVAL
    assert _sleep(_at(61), last_run=T0, min_delay=0) == 0


def test_an_attempt_newer_than_the_last_run_is_the_anchor():
    """A tick that started and was killed mid-analysis left no run row. Its
    attempt mark must count, or every restart re-runs the analysis job."""
    killed_at = _at(70)
    assert _sleep(_at(72), last_run=T0, last_attempt=killed_at) == 58 * 60
    assert _sleep(_at(72), last_attempt=killed_at) == 58 * 60


def test_an_attempt_older_than_the_last_run_is_ignored():
    assert _sleep(_at(80), last_run=_at(70), last_attempt=_at(60)) == 50 * 60


def test_a_timestamp_from_the_future_never_sleeps_past_one_interval():
    """DB clock ahead of ours: treat the run as having just happened."""
    assert _sleep(_at(0), last_run=_at(600)) == INTERVAL


def test_floor_is_capped_at_the_interval():
    """A 30 s loop with a 90 s startup delay must not become a 90 s loop."""
    got = seconds_until_next_run(now=T0, interval_seconds=30, min_delay_seconds=90, last_run_at=None)
    assert got == 30


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_interval_is_refused(bad: int):
    with pytest.raises(ValueError):
        seconds_until_next_run(now=T0, interval_seconds=bad, min_delay_seconds=DELAY)


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError):
        seconds_until_next_run(now=datetime(2026, 9, 20), interval_seconds=INTERVAL, min_delay_seconds=DELAY)


# ---- restart sequences ------------------------------------------------------


class _World:
    """A durable store (last run, last attempt) and a process that restarts."""

    def __init__(self, last_run: datetime | None) -> None:
        self.last_run = last_run
        self.last_attempt: datetime | None = None
        self.ticks: list[datetime] = []

    def boot(self, now: datetime) -> datetime:
        """A process starts at ``now``; returns when its first tick would fire."""
        return now + timedelta(seconds=_sleep(now, last_run=self.last_run, last_attempt=self.last_attempt))

    def tick(self, at: datetime, *, killed: bool = False) -> None:
        self.last_attempt = at
        if not killed:
            self.last_run = at
            self.ticks.append(at)


def test_the_2026_09_20_deploy_burst_no_longer_silences_the_watcher():
    """Ticked 04:14; five deploys 04:22-05:22. Sleep-first next ticked 06:22."""
    world = _World(last_run=T0)
    restarts = [_at(m) for m in (8, 23, 38, 53, 68)]  # 04:22 … 05:22
    for now, next_restart in zip(restarts, [*restarts[1:], _at(600)], strict=True):
        fire = world.boot(now)
        if fire < next_restart:
            world.tick(fire)
    # 05:14 on schedule between the fourth and fifth deploy, then hourly.
    assert world.ticks == [_at(60), _at(120)]


def test_three_restarts_never_push_ticks_further_apart_than_interval_plus_delay():
    bound = timedelta(seconds=INTERVAL + DELAY)
    # Restarts anywhere inside the interval, including the last second of it.
    for restarts in [(1, 2, 3), (10, 30, 59), (59, 59.5, 59.99), (0.1, 45, 59.99)]:
        world = _World(last_run=T0)
        fire = None
        for minute in restarts:
            fire = world.boot(_at(minute))
        assert fire is not None
        assert fire - T0 <= bound, restarts
        assert fire >= T0 + timedelta(seconds=INTERVAL), "a restart must not bring the tick forward"


def test_a_crash_loop_produces_one_tick_per_interval_not_one_per_restart():
    """Overdue at boot, and every process dies ten minutes into its analysis
    job. Without the attempt mark each of these restarts would start a job."""
    world = _World(last_run=T0 - timedelta(hours=5))
    now = T0
    started: list[datetime] = []
    for _ in range(12):  # twelve restarts, ~12 minutes apart
        fire = world.boot(now)
        died = now + timedelta(minutes=12)
        if fire < died:
            started.append(fire)
            world.tick(fire, killed=True)
        now = died
    assert len(started) == 3  # 144 minutes of crash loop: t+1.5, +61.5, +121.5
    gaps = [b - a for a, b in zip(started, started[1:], strict=False)]
    assert all(gap >= timedelta(seconds=INTERVAL) for gap in gaps)


def test_a_crash_loop_faster_than_the_startup_delay_never_ticks():
    """The stated limit of the bound: a process that lives under the startup
    delay cannot tick — and could not finish an analysis job either."""
    world = _World(last_run=T0 - timedelta(hours=5))
    assert all(world.boot(_at(m)) > _at(m + 1) for m in range(10))
