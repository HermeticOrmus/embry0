"""Periodic sweep for labeled Linear tickets that never got a job (#284).

The Linear trigger is a webhook, and a webhook that is lost — the tunnel was
down, the orchestrator was mid-deploy, Linear gave up retrying — is lost for
good: the ticket keeps its label and nothing ever looks at it again. The
reconciler is the second look. On an interval it lists the dispatchable
tickets that carry the trigger label and hands each to the *same* dispatch
method the webhook uses, so eligibility, repo resolution, dedupe and the
ticket comments are one implementation, not two.

What a sweep may start:

* a ticket embry0 has never tracked, or tracks without any job (a dispatch
  that failed and was reverted to ``open``);
* a ticket whose latest job ended failed/cancelled/expired **and** whose
  trigger label was applied again after that job — the missed-webhook twin of
  the relabel rule. A label that merely stayed on a failed ticket is not a
  request: retrying it every interval would be an unattended spend loop.

Nobody is watching when a sweep dispatches, so it is bounded four ways, all
counted from durable state (they hold across restarts):

* ``max_dispatches_per_sweep`` — jobs one sweep may start;
* ``max_dispatches_per_day`` — jobs the reconciler started in the last 24 h;
* ``max_attempts_per_ticket`` — jobs the reconciler ever started for a ticket;
* ``failure_max_attempts`` with exponential backoff — dispatches that raised
  before a job existed (they cost nothing, but an unbounded retry rewrites the
  row and warns on every sweep forever).

Its jobs are attributed to the fixed actor :data:`ACTOR`, so the fleet-wide
per-author rate limit applies to it like to any human.

**Modes** (``LINEAR_RECONCILE_MODE``): ``off`` never runs; ``shadow`` — the
default, matching the staged-autonomy plan where a human promotes a stage —
takes every decision and changes nothing: no row, no job, no ticket comment,
no reopened issue. It logs what it *would* do, once per ticket and again only
when the decision changes. ``on`` acts.

The interface is deliberately two methods: :meth:`run_once` (one sweep, also
what tests drive) and :meth:`start` (the interval loop).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from embry0.metrics import NULL_METRICS, LoopMetrics
from embry0.services import linear_notice_texts as texts
from embry0.services.linear_sync import AdmissionRefusal, LinearTriggerIssue, label_reapplied_since
from embry0.services.loop_schedule import seconds_until_next_run
from embry0.trackers import TriggerContext

logger = structlog.get_logger(__name__)

# issues.created_by for rows the sweep creates AND jobs.dispatched_by for the
# jobs it starts — the latter is what its own bounds are counted from.
ACTOR = "linear-reconciler"
TRACKER = "linear"
MODES = ("off", "shadow", "on")

# The backoff after a dispatch that raised doubles from the sweep interval up
# to this ceiling.
_MAX_FAILURE_BACKOFF = timedelta(hours=6)


@dataclass(frozen=True)
class ReconcilerSettings:
    """Every knob of the reconciler — see ``docs/configuration.md``."""

    mode: str = "shadow"
    interval_seconds: int = 300
    startup_delay_seconds: int = 90
    max_dispatches_per_sweep: int = 3
    max_dispatches_per_day: int = 5
    max_attempts_per_ticket: int = 2
    failure_max_attempts: int = 5
    stale_triaging_minutes: int = 15

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"LINEAR_RECONCILE_MODE must be one of {MODES}, got {self.mode!r}")


class LinearDispatchReconciler:
    """Sweeps labeled Linear tickets into the shared dispatch path.

    ``resolve_sync`` yields the current ``LinearSyncService`` or None when
    Linear is not configured (credentials are dashboard-editable, so this is
    asked every sweep rather than fixed at startup). ``resolve_trigger_labels``
    yields the effective labels — DB integration config over env.
    ``make_context`` builds the ``TriggerContext`` for those labels; its
    repositories are also where the bounds are counted from.
    """

    def __init__(
        self,
        *,
        resolve_sync: Callable[[], Awaitable[Any | None]],
        resolve_trigger_labels: Callable[[], Awaitable[set[str]]],
        make_context: Callable[[set[str]], TriggerContext],
        settings: ReconcilerSettings,
        clock: Callable[[], datetime] | None = None,
        metrics: LoopMetrics | None = None,
    ) -> None:
        self._metrics = metrics or NULL_METRICS
        self._resolve_sync = resolve_sync
        self._resolve_trigger_labels = resolve_trigger_labels
        self._make_context = make_context
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        # A sweep that outlives the interval (slow Linear) must not overlap
        # the next one; run_once is also reachable outside the loop.
        self._sweep_lock = asyncio.Lock()
        # Shadow mode: the last decision logged per subject, so an unchanged
        # ticket is not re-logged every interval. In memory on purpose — after
        # a restart each ticket is logged once more, which is a feature for
        # whoever is reading the new process's log.
        self._shadow_logged: dict[str, tuple[Any, ...]] = {}
        # #285: the last decision COUNTED per ticket. A standing decision is
        # taken again every sweep — at a five-minute interval one stale ticket
        # would otherwise add ~2,016 increments a week, and `increase()` would
        # read "288 tickets" where it means "one ticket, 288 times". Same
        # change-detection as the shadow log, kept apart because the two carry
        # different tuples for the same subject.
        self._counted: dict[str, tuple[str, str]] = {}
        self._metrics.reconciler_configured(
            enabled=self.enabled, max_dispatches_per_day=settings.max_dispatches_per_day
        )

    @property
    def enabled(self) -> bool:
        """False for mode ``off`` or a non-positive interval — the loop is never started."""
        return self._settings.mode != "off" and self._settings.interval_seconds > 0

    @property
    def acts(self) -> bool:
        """True in ``on`` mode with a running loop — tickets are retried without a human."""
        return self.enabled and self._settings.mode == "on"

    async def run_once(self) -> dict[str, Any]:
        """One sweep. Returns counts; ``status`` is ``disabled`` when there is nothing to run."""
        mode = self._settings.mode
        if mode == "off":
            return {"status": "disabled", "reason": "LINEAR_RECONCILE_MODE is off"}
        try:
            result, skipped = await self._sweep()
        except Exception:
            self._metrics.reconciler_sweep(mode=mode, result="error")
            raise
        if result["status"] != "ok":
            self._metrics.reconciler_sweep(mode=mode, result="disabled")
            return result
        self._metrics.reconciler_sweep(
            mode=mode,
            result="ok",
            # The standing view of this sweep. A `decision` shared with
            # embry0_linear_reconciler_decisions_total means the same there;
            # listed/deferred/reopened are sweep totals, which that per-ticket
            # counter has no equivalent for.
            tickets={
                "listed": result["listed"],
                "dispatched": len(result["dispatched"]),
                "would_dispatch": len(result["would_dispatch"]),
                "skipped": skipped,
                "failed": result["failed"],
                "deferred": result["deferred"],
                "reopened": len(result["reopened"]),
                "would_reopen": len(result["would_reopen"]),
            },
        )
        return result

    async def _sweep(self) -> tuple[dict[str, Any], int]:
        """The sweep itself: ``run_once``'s result, plus how many tickets it skipped."""
        shadow = self._settings.mode != "on"
        async with self._sweep_lock:
            sync = await self._resolve_sync()
            if sync is None:
                return {"status": "disabled", "reason": "linear integration not configured"}, 0
            labels = await self._resolve_trigger_labels()
            ctx = self._make_context(labels)
            stale = await self._heal_stale_triaging(ctx, shadow=shadow)
            issues = await sync.list_trigger_issues(labels)

            dispatched: list[str] = []
            would_dispatch: list[str] = []
            failed = 0
            skipped = 0
            deferred = 0
            seen: set[str] = {f"reopen:{key}" for key in stale}
            for position, issue in enumerate(issues):
                if len(dispatched) >= self._settings.max_dispatches_per_sweep:
                    deferred = len(issues) - position
                    break
                seen.add(issue.key)
                try:
                    result = await sync.dispatch_trigger_issue(
                        issue,
                        ctx,
                        created_by=ACTOR,
                        label_reapplied=label_reapplied_since(sync, issue.native_id or issue.key, labels),
                        announce_dedupe=False,
                        admit=self._admission(ctx),
                        dry_run=shadow,
                        # Shadow did not reopen the stale row; judge the ticket
                        # as `on` would find it, one step later.
                        assume_reopened=shadow and issue.key in stale,
                    )
                except Exception:
                    # One unreadable ticket must not end the sweep for the rest.
                    failed += 1
                    self._count_decision(issue.key, "failed", "exception")
                    logger.warning("linear_reconcile_ticket_failed", identifier=issue.key, exc_info=True)
                    continue
                if result.get("action") == "dispatched":
                    dispatched.append(issue.key)
                    # An action, not a standing state: counted every time, and
                    # it clears the memo so a later retry of the same ticket
                    # counts again.
                    self._counted.pop(issue.key, None)
                    self._metrics.reconciler_decision("dispatched", result.get("basis"))
                    logger.info("linear_reconcile_dispatched", identifier=issue.key, job_id=result.get("job_id"))
                elif result.get("status") == "would_dispatch":
                    would_dispatch.append(issue.key)
                    self._count_decision(issue.key, "would_dispatch", result.get("basis"))
                else:
                    skipped += 1
                    self._count_decision(issue.key, "skipped", result.get("skip"))
                if shadow and result.get("action") != "dispatched":
                    await self._log_shadow_decision(ctx, issue, result)

            if not deferred:
                # Forget tickets that left the listing, so one that comes back
                # is logged and counted as new. Skipped on a truncated sweep:
                # unseen is not gone.
                self._shadow_logged = {k: v for k, v in self._shadow_logged.items() if k in seen}
                self._counted = {k: v for k, v in self._counted.items() if k in seen}
            return {
                "status": "ok",
                "mode": self._settings.mode,
                "listed": len(issues),
                "dispatched": dispatched,
                "would_dispatch": would_dispatch,
                # One of the two is always empty: shadow reports, `on` acts.
                "reopened": [] if shadow else stale,
                "would_reopen": stale if shadow else [],
                "failed": failed,
                "deferred": deferred,
            }, skipped

    async def _sleep_until_next_sweep(self, last_sweep_at: datetime | None) -> None:
        """Wait until the next sweep is due (#335); never raises.

        The loop task is fire-and-forget (``add_done_callback(discard)``), so
        anything escaping the scheduling call would stop the reconciler with
        no log line at all. A schedule that cannot be computed falls back to
        one interval — the behaviour before #335.
        """
        try:
            delay = seconds_until_next_run(
                now=self._clock(),
                interval_seconds=self._settings.interval_seconds,
                min_delay_seconds=self._settings.startup_delay_seconds if last_sweep_at is None else 0,
                last_run_at=last_sweep_at,
            )
        except Exception:
            logger.error("linear_reconcile_schedule_failed", exc_info=True)
            delay = float(self._settings.interval_seconds)
        await asyncio.sleep(delay)

    async def start(self) -> None:
        """Interval loop; every iteration is isolated so one bad sweep is one log line."""
        logger.info("linear_reconciler_started", **self._settings.__dict__)
        # #335: the first sweep waits the startup delay, not a whole interval,
        # so a day of deploys does not starve the second look. "Last sweep" is
        # kept in memory on purpose, unlike the watcher's tick: a sweep costs
        # one Linear query, is idempotent, and everything it may START is
        # bounded from the jobs table and the dispatch ledger — a sweep per
        # restart of a crash-looping orchestrator spends nothing.
        last_sweep_at: datetime | None = None
        while True:
            await self._sleep_until_next_sweep(last_sweep_at)
            try:
                result = await self.run_once()
                if result["status"] == "ok" and (
                    result["dispatched"] or result["reopened"] or result["failed"] or result["deferred"]
                ):
                    logger.info("linear_reconcile_sweep", **result)
            except Exception:
                logger.error("linear_reconcile_sweep_failed", exc_info=True)
            finally:
                last_sweep_at = self._clock()

    # ---- bounds -----------------------------------------------------------

    async def _usage(self, ctx: TriggerContext, tracker_key: str) -> tuple[int, int]:
        """(jobs the reconciler started in the last 24 h, jobs it ever started for this ticket)."""
        daily = await ctx.jobs_repo.count_dispatched_by(ACTOR, since=self._clock() - timedelta(days=1))
        attempts = await ctx.jobs_repo.count_dispatched_by(ACTOR, tracker=TRACKER, tracker_key=tracker_key)
        return int(daily), int(attempts)

    def _admission(self, ctx: TriggerContext) -> Callable[[LinearTriggerIssue], Awaitable[AdmissionRefusal | None]]:
        """The reconciler's own admission policy, asked just before a dispatch."""
        settings = self._settings

        async def _admit(issue: LinearTriggerIssue) -> AdmissionRefusal | None:
            daily, attempts = await self._usage(ctx, issue.key)
            if attempts >= settings.max_attempts_per_ticket:
                return AdmissionRefusal(
                    "reconciler_ticket_attempts", texts.reconciler_ticket_attempts(attempts), announce=True
                )
            if daily >= settings.max_dispatches_per_day:
                return AdmissionRefusal(
                    "reconciler_daily_cap",
                    f"the reconciler already started {daily} job(s) in the last 24 h (cap {settings.max_dispatches_per_day})",
                )
            state = await ctx.dispatch_ledger.failure_state(TRACKER, issue.key) if ctx.dispatch_ledger else None
            failures = int((state or {}).get("failures") or 0)
            if failures >= settings.failure_max_attempts:
                return AdmissionRefusal(
                    "reconciler_dispatch_failures", texts.reconciler_dispatch_failures(failures), announce=True
                )
            if failures and state is not None:
                wait = min(timedelta(seconds=settings.interval_seconds) * 2 ** (failures - 1), _MAX_FAILURE_BACKOFF)
                last = state["last_failed_at"]
                last = last if last.tzinfo else last.replace(tzinfo=UTC)
                if self._clock() < last + wait:
                    return AdmissionRefusal(
                        "reconciler_failure_backoff",
                        f"backing off after {failures} failed dispatch(es); next try after {(last + wait).isoformat()}",
                    )
            return None

        return _admit

    # ---- stale `triaging` --------------------------------------------------

    async def _heal_stale_triaging(self, ctx: TriggerContext, *, shadow: bool) -> list[str]:
        """Reopen Linear rows left ``triaging`` with no live job (see ``stale_triaging``).

        Returns the ticket keys found; in shadow mode they are reported, not reopened.
        """
        minutes = self._settings.stale_triaging_minutes
        if minutes <= 0:
            return []
        rows = await ctx.issues_repo.stale_triaging(TRACKER, minutes, reopen=not shadow)
        for row in rows:
            key = str(row.get("tracker_key") or row["id"])
            if not shadow:
                logger.warning("linear_reconcile_reopened_stale_triaging", identifier=key, issue_id=row["id"])
            elif self._shadow_changed(f"reopen:{key}", ("would_reopen",)):
                logger.info(
                    "linear_reconcile_would_reopen",
                    identifier=key,
                    issue_id=row["id"],
                    reason="stale_triaging",
                    older_than_minutes=minutes,
                )
        return [str(row.get("tracker_key") or row["id"]) for row in rows]

    # ---- metrics -----------------------------------------------------------

    def _count_decision(self, key: str, decision: str, reason: object) -> None:
        """Count a STANDING decision only when it changes for that ticket."""
        current = (decision, str(reason or "unknown"))
        if self._counted.get(key) == current:
            return
        self._counted[key] = current
        self._metrics.reconciler_decision(*current)

    # ---- shadow log --------------------------------------------------------

    def _shadow_changed(self, subject: str, decision: tuple[Any, ...]) -> bool:
        if self._shadow_logged.get(subject) == decision:
            return False
        self._shadow_logged[subject] = decision
        return True

    async def _log_shadow_decision(
        self, ctx: TriggerContext, issue: LinearTriggerIssue, result: dict[str, Any]
    ) -> None:
        """One structured line per ticket — on first sight and when the decision changes."""
        if result.get("status") == "would_dispatch":
            decision = ("would_dispatch", result.get("repo"), result.get("basis"), result.get("after_reopen"))
            if not self._shadow_changed(issue.key, decision):
                return
            daily, attempts = await self._usage(ctx, issue.key)
            logger.info(
                "linear_reconcile_would_dispatch",
                identifier=issue.key,
                repo=result.get("repo"),
                reason=result.get("basis"),
                # True: `on` would first reopen this ticket's stale `triaging`
                # row (see linear_reconcile_would_reopen), then dispatch it.
                after_reopen=bool(result.get("after_reopen")),
                daily_dispatches=daily,
                daily_cap=self._settings.max_dispatches_per_day,
                ticket_attempts=attempts,
                ticket_attempt_cap=self._settings.max_attempts_per_ticket,
            )
            return
        skip = result.get("skip") or "unknown"
        if self._shadow_changed(issue.key, ("would_skip", skip)):
            logger.info(
                "linear_reconcile_would_skip",
                identifier=issue.key,
                reason=skip,
                detail=result.get("detail") or result.get("reason") or "",
            )
