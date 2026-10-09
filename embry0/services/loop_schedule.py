"""When an interval loop should next run (#335).

A loop that sleeps its interval before the first run restarts its clock with
the process: five deploys in an hour silenced the hourly watcher for two, and
a crash-looping orchestrator silences it for good. The decision here is
anchored on what is *recorded* instead — the last completed run and the last
attempt — so a restart neither postpones a due run nor repeats a recent one.

Pure on purpose: no clock, no I/O. Callers read their anchors (a table, or
memory when a run is cheap and idempotent), pass ``now``, and sleep the result.
"""

from __future__ import annotations

from datetime import datetime


def seconds_until_next_run(
    *,
    now: datetime,
    interval_seconds: float,
    min_delay_seconds: float,
    last_run_at: datetime | None = None,
    last_attempt_at: datetime | None = None,
) -> float:
    """Seconds to sleep before the next run.

    - ``last_run_at``: when a run last completed (left its record).
    - ``last_attempt_at``: when a run last *started*. A run killed midway
      completes nothing, so without this a restart re-runs it at once, every
      restart. The newer of the two is the anchor; the next run is due one
      interval after it. No anchor at all means overdue.
    - ``min_delay_seconds``: the floor. Callers pass their startup delay for
      the first run of a process (a deploy must not compete with the run, and
      start-up checks need a verdict first) and ``0`` afterwards. Capped at
      the interval, so a short loop is never slowed by a long startup delay.

    An anchor later than ``now`` (the recording clock runs ahead) counts as
    "just now": the result never exceeds one interval, and never is negative.

    Consequence, for a process that outlives its startup delay: a restart
    cannot move two consecutive runs further apart than interval +
    ``min_delay_seconds``, and cannot bring the next run forward at all.
    """
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be positive, got {interval_seconds!r}")
    if now.tzinfo is None or any(t is not None and t.tzinfo is None for t in (last_run_at, last_attempt_at)):
        raise ValueError("seconds_until_next_run needs timezone-aware datetimes")

    floor = min(max(0.0, float(min_delay_seconds)), float(interval_seconds))
    anchors = [t for t in (last_run_at, last_attempt_at) if t is not None]
    if not anchors:
        return floor
    elapsed = max(0.0, (now - max(anchors)).total_seconds())
    return max(floor, float(interval_seconds) - elapsed)
