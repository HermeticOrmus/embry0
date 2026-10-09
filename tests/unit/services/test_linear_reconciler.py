"""LinearDispatchReconciler — the sweep for labeled tickets with no job (#284).

Drives the real ``LinearSyncService.dispatch_trigger_issue`` against a small
in-memory world (issues, jobs, executor) so the reconciler and the webhook are
exercised through the one path they share. Linear itself is never called: the
service's reads and its comment write are replaced per test.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from embry0.services.linear_reconciler import ACTOR, LinearDispatchReconciler, ReconcilerSettings
from embry0.services.linear_sync import LinearSyncService, LinearTriggerIssue
from embry0.storage.repositories.jobs import StatusTransitionConflict
from embry0.trackers import TriggerContext

_FAILED_AT = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def _ticket(
    key: str, *, labels: tuple[str, ...] = ("embry0",), summary: str = "repo: o/r", state_type: str = "started"
) -> LinearTriggerIssue:
    return LinearTriggerIssue(
        tracker="linear",
        native_id=f"uuid-{key}",
        key=key,
        title="Fix it",
        body="",
        url=f"https://linear.app/x/issue/{key}",
        labels=list(labels),
        project="Proj",
        team_key="ENG",
        project_summary=summary,
        state_type=state_type,
    )


class _World:
    """Issues + jobs + executor with the two properties the race depends on:
    every call yields to the event loop, and ``open -> triaging`` is a CAS."""

    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.jobs: list[dict[str, Any]] = []
        self.execute_error: Exception | None = None
        self.gate_actors: list[str | None] = []

    # -- issues repo
    async def get_by_tracker_ref(self, tracker: str, repo: str | None, key: str) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return next((dict(i) for i in self.issues.values() if i.get("tracker_key") == key), None)

    async def create(self, **kw: Any) -> str:
        await asyncio.sleep(0)
        issue_id = f"iss-{len(self.issues) + 1}"
        self.issues[issue_id] = {"id": issue_id, "status": "open", **kw}
        return issue_id

    async def update(self, issue_id: str, **kw: Any) -> None:
        await asyncio.sleep(0)
        row = self.issues[issue_id]
        if kw.get("status") == "triaging" and row["status"] != "open":
            raise StatusTransitionConflict("not open")
        row.update(kw)

    # -- jobs repo
    async def list_all(self, issue_id: str | None = None, **kw: Any) -> tuple[list[dict[str, Any]], int]:
        await asyncio.sleep(0)
        rows = [j for j in reversed(self.jobs) if j["issue_id"] == issue_id]
        return rows, len(rows)

    # -- executor
    async def execute(self, issue_id: str, **kw: Any) -> str:
        await asyncio.sleep(0)
        if self.execute_error is not None:
            raise self.execute_error
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs.append(
            {"job_id": job_id, "issue_id": issue_id, "status": "pending", "created_at": datetime.now(UTC), **kw}
        )
        return job_id

    async def dispatch_refusal(self, *, actor: str | None = None) -> None:
        self.gate_actors.append(actor)
        return None

    # -- the durable counters the reconciler's bounds read
    async def count_dispatched_by(
        self, actor: str, *, since: datetime | None = None, tracker: str | None = None, tracker_key: str | None = None
    ) -> int:
        def _matches(job: dict[str, Any]) -> bool:
            if job.get("dispatched_by") != actor:
                return False
            if since is not None and job["created_at"] < since:
                return False
            return tracker_key is None or self.issues[job["issue_id"]].get("tracker_key") == tracker_key

        return sum(_matches(job) for job in self.jobs)

    async def stale_triaging(self, tracker: str, older_than_minutes: int, *, reopen: bool) -> list[dict[str, Any]]:
        live = {"pending", "running", "awaiting_input", "paused"}
        cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
        rows = [
            row
            for row in self.issues.values()
            if row["status"] == "triaging"
            and row.get("tracker") == tracker
            and row.get("updated_at", datetime.now(UTC)) < cutoff
            and not any(j["issue_id"] == row["id"] and j["status"] in live for j in self.jobs)
        ]
        if reopen:
            for row in rows:
                row["status"] = "open"
        return [{"id": row["id"], "tracker_key": row.get("tracker_key")} for row in rows]

    def track(
        self,
        key: str,
        *,
        status: str,
        job_status: str | None,
        pr_url: str | None = None,
        dispatched_by: str | None = None,
        updated_at: datetime | None = None,
    ) -> str:
        issue_id = f"iss-{len(self.issues) + 1}"
        self.issues[issue_id] = {
            "id": issue_id,
            "status": status,
            "tracker": "linear",
            "tracker_key": key,
            "updated_at": updated_at or datetime.now(UTC),
        }
        if job_status is not None:
            self.jobs.append(
                {
                    "job_id": f"job-{len(self.jobs) + 1}",
                    "issue_id": issue_id,
                    "status": job_status,
                    "pr_url": pr_url,
                    "finished_at": _FAILED_AT,
                    "created_at": datetime.now(UTC),
                    "dispatched_by": dispatched_by,
                }
            )
        return issue_id


class _Ledger:
    def __init__(self) -> None:
        self.claimed: set[tuple[str, str, str]] = set()
        self.failures: dict[str, dict[str, Any]] = {}
        self.clock: Any = lambda: datetime.now(UTC)

    async def claim(self, tracker: str, key: str, reason: str) -> bool:
        if (tracker, key, reason) in self.claimed:
            return False
        self.claimed.add((tracker, key, reason))
        return True

    async def release(self, tracker: str, key: str, reason: str) -> None:
        self.claimed.discard((tracker, key, reason))

    async def record_failure(self, tracker: str, key: str) -> int:
        state = self.failures.setdefault(key, {"failures": 0, "last_failed_at": None})
        state["failures"] += 1
        state["last_failed_at"] = self.clock()
        return int(state["failures"])

    async def failure_state(self, tracker: str, key: str) -> dict[str, Any] | None:
        return self.failures.get(key)

    async def clear(self, tracker: str, key: str) -> None:
        self.claimed = {c for c in self.claimed if c[1] != key}
        self.failures.pop(key, None)


def _sync(tickets: list[LinearTriggerIssue], *, reapplied: bool = False) -> LinearSyncService:
    svc = LinearSyncService("lin_api_key", {})
    svc.list_trigger_issues = AsyncMock(return_value=tickets)  # type: ignore[method-assign]
    svc.trigger_label_added_since = AsyncMock(return_value=reapplied)  # type: ignore[method-assign]
    svc.post_comment = AsyncMock(return_value=True)  # type: ignore[method-assign]
    svc._graphql = AsyncMock(side_effect=AssertionError("the real Linear API must never be called"))  # type: ignore[method-assign]
    return svc


def _ctx(world: _World, labels: set[str], ledger: _Ledger | None = None, actor: str = "") -> TriggerContext:
    return TriggerContext(
        issues_repo=world,
        issue_executor=world,
        jobs_repo=world,
        dispatch_ledger=ledger,
        trigger_labels=labels,
        actor=actor,
    )


def _reconciler(
    world: _World,
    sync: LinearSyncService | None,
    *,
    labels: set[str] | None = None,
    ledger: _Ledger | None = None,
    mode: str = "on",
    clock: Any = None,
    **settings: int,
) -> LinearDispatchReconciler:
    """``mode`` defaults to ``on`` here so the dispatch tests read plainly; the
    shipped default is ``shadow`` (see the mode tests)."""
    effective = labels if labels is not None else {"embry0"}
    return LinearDispatchReconciler(
        resolve_sync=AsyncMock(return_value=sync),
        resolve_trigger_labels=AsyncMock(return_value=effective),
        make_context=lambda resolved: _ctx(world, resolved, ledger, actor=ACTOR),
        settings=ReconcilerSettings(mode=mode, **settings),
        clock=clock,
    )


async def test_sweep_dispatches_a_labeled_ticket_the_webhook_never_delivered() -> None:
    world, sync = _World(), _sync([_ticket("ENG-1")])

    result = await _reconciler(world, sync).run_once()

    assert result["status"] == "ok" and result["dispatched"] == ["ENG-1"]
    assert [j["status"] for j in world.jobs] == ["pending"]
    row = next(iter(world.issues.values()))
    assert row["created_by"] == "linear-reconciler" and row["status"] == "triaging"
    # A fixed actor: dispatched_by is populated and the per-author rate limit sees it.
    assert world.jobs[0]["dispatched_by"] == ACTOR == "linear-reconciler"
    assert world.gate_actors == [ACTOR]


async def test_a_second_sweep_is_idempotent() -> None:
    world, sync = _World(), _sync([_ticket("ENG-1")])
    reconciler = _reconciler(world, sync)

    await reconciler.run_once()
    second = await reconciler.run_once()

    assert second["dispatched"] == [] and len(world.jobs) == 1 and len(world.issues) == 1


async def test_sweep_uses_the_effective_trigger_labels() -> None:
    """The DB integration config overrides env — the resolver's answer is what is listed and matched."""
    world = _World()
    sync = _sync([_ticket("ENG-1", labels=("autofix",)), _ticket("ENG-2", labels=("embry0",))])

    result = await _reconciler(world, sync, labels={"autofix"}).run_once()

    sync.list_trigger_issues.assert_awaited_once_with({"autofix"})  # type: ignore[attr-defined]
    assert result["dispatched"] == ["ENG-1"]


@pytest.mark.parametrize(
    ("issue_status", "job_status", "pr_url"),
    [
        ("triaging", "running", None),
        ("triaging", "pending", None),
        ("awaiting_input", "awaiting_input", None),
        ("closed", "completed", "https://github.com/o/r/pull/3"),
        ("closed", "pr_merged", "https://github.com/o/r/pull/3"),
    ],
)
async def test_sweep_leaves_handled_tickets_alone_and_silent(issue_status, job_status, pr_url) -> None:
    world, ledger = _World(), _Ledger()
    world.track("ENG-1", status=issue_status, job_status=job_status, pr_url=pr_url)
    sync = _sync([_ticket("ENG-1")], reapplied=True)

    result = await _reconciler(world, sync, ledger=ledger).run_once()

    assert result["dispatched"] == [] and len(world.jobs) == 1
    sync.post_comment.assert_not_called()  # type: ignore[attr-defined]
    assert ledger.claimed == set()


async def test_sweep_dispatches_a_stranded_row_that_has_no_job() -> None:
    world = _World()
    issue_id = world.track("ENG-1", status="open", job_status=None)

    result = await _reconciler(world, _sync([_ticket("ENG-1")])).run_once()

    assert result["dispatched"] == ["ENG-1"]
    assert world.jobs[0]["issue_id"] == issue_id and len(world.issues) == 1


async def test_a_failed_ticket_is_retried_only_when_the_label_was_reapplied_after_it() -> None:
    world = _World()
    world.track("ENG-1", status="open", job_status="failed")
    stale = _sync([_ticket("ENG-1")], reapplied=False)

    assert (await _reconciler(world, stale).run_once())["dispatched"] == []
    assert len(world.jobs) == 1
    stale.trigger_label_added_since.assert_awaited_once_with("uuid-ENG-1", {"embry0"}, _FAILED_AT)  # type: ignore[attr-defined]

    relabeled = _sync([_ticket("ENG-1")], reapplied=True)
    assert (await _reconciler(world, relabeled).run_once())["dispatched"] == ["ENG-1"]
    assert [j["status"] for j in world.jobs] == ["failed", "pending"]


async def test_dispatches_per_sweep_are_capped() -> None:
    world = _World()
    sync = _sync([_ticket(f"ENG-{n}") for n in range(1, 6)])
    reconciler = _reconciler(world, sync, max_dispatches_per_sweep=2, max_dispatches_per_day=50)

    first = await reconciler.run_once()
    second = await reconciler.run_once()

    assert first["dispatched"] == ["ENG-1", "ENG-2"] and first["deferred"] == 3
    assert second["dispatched"] == ["ENG-3", "ENG-4"]
    assert len(world.jobs) == 4


async def test_unmapped_ticket_is_commented_once_across_sweeps() -> None:
    world, ledger = _World(), _Ledger()
    sync = _sync([_ticket("ENG-1", summary="")])
    reconciler = _reconciler(world, sync, ledger=ledger)

    await reconciler.run_once()
    await reconciler.run_once()

    assert world.issues == {} and world.jobs == []
    assert sync.post_comment.await_count == 1  # type: ignore[attr-defined]
    assert ("linear", "ENG-1", "unmapped_repo") in ledger.claimed


async def test_one_broken_ticket_does_not_end_the_sweep() -> None:
    world = _World()
    sync = _sync([_ticket("ENG-1"), _ticket("ENG-2")])
    real = sync.dispatch_trigger_issue

    async def _flaky(issue: LinearTriggerIssue, *a: Any, **kw: Any) -> dict[str, Any]:
        if issue.key == "ENG-1":
            raise RuntimeError("db hiccup")
        return await real(issue, *a, **kw)

    sync.dispatch_trigger_issue = _flaky  # type: ignore[method-assign]

    result = await _reconciler(world, sync).run_once()

    assert result["failed"] == 1 and result["dispatched"] == ["ENG-2"]


async def test_a_failed_dispatch_is_reverted_and_picked_up_by_the_next_sweep() -> None:
    world, sync = _World(), _sync([_ticket("ENG-1")])
    reconciler = _reconciler(world, sync)
    world.execute_error = RuntimeError("sandbox backend down")

    first = await reconciler.run_once()
    world.execute_error = None
    second = await reconciler.run_once()

    assert first["dispatched"] == [] and second["dispatched"] == ["ENG-1"]
    assert len(world.issues) == 1 and len(world.jobs) == 1


@pytest.mark.parametrize("state_type", ["duplicate", "triage", "completed", "canceled", ""])
async def test_sweep_never_dispatches_from_a_non_dispatchable_state(state_type: str) -> None:
    """The list query filters by state; the dispatch path must not rely on it."""
    world, ledger = _World(), _Ledger()
    sync = _sync([_ticket("ENG-1", state_type=state_type)])

    result = await _reconciler(world, sync, ledger=ledger).run_once()

    assert result["dispatched"] == [] and world.issues == {} and world.jobs == []
    sync.post_comment.assert_not_called()  # type: ignore[attr-defined]


# ---- disabled cleanly ------------------------------------------------------


async def test_sweep_is_a_no_op_when_linear_is_not_configured() -> None:
    world = _World()
    reconciler = _reconciler(world, None)

    result = await reconciler.run_once()

    assert result == {"status": "disabled", "reason": "linear integration not configured"}
    assert world.issues == {} and world.jobs == []


@pytest.mark.parametrize(
    ("mode", "interval", "enabled"),
    [("on", 0, False), ("on", -5, False), ("on", 60, True), ("shadow", 60, True), ("off", 60, False)],
)
def test_mode_off_or_a_zero_interval_disables_the_loop(mode: str, interval: int, enabled: bool) -> None:
    assert _reconciler(_World(), None, mode=mode, interval_seconds=interval).enabled is enabled


async def test_mode_off_never_sweeps() -> None:
    world, sync = _World(), _sync([_ticket("ENG-1")])

    result = await _reconciler(world, sync, mode="off").run_once()

    assert result["status"] == "disabled"
    sync.list_trigger_issues.assert_not_called()  # type: ignore[attr-defined]


def test_the_shipped_default_is_shadow_with_conservative_bounds() -> None:
    from embry0.config import Embry0Config

    settings = ReconcilerSettings()
    assert settings.mode == "shadow"
    assert (settings.max_dispatches_per_day, settings.max_attempts_per_ticket) == (5, 2)
    fields = Embry0Config.model_fields
    assert fields["linear_reconcile_mode"].default == "shadow"
    assert fields["linear_reconcile_max_dispatches_per_day"].default == 5
    assert fields["linear_reconcile_max_attempts_per_ticket"].default == 2


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="LINEAR_RECONCILE_MODE"):
        ReconcilerSettings(mode="yolo")


async def test_the_loop_survives_a_failing_sweep(monkeypatch) -> None:
    reconciler = _reconciler(_World(), None, interval_seconds=1)
    calls = 0

    async def _run_once() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("linear 502")
        raise asyncio.CancelledError

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(reconciler, "run_once", _run_once)
    monkeypatch.setattr("embry0.services.linear_reconciler.asyncio.sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await reconciler.start()

    assert calls == 2


async def test_first_sweep_after_a_start_waits_the_startup_delay_not_an_interval(monkeypatch) -> None:
    """#335: a restart used to cost a whole interval. The reconciler keeps no
    "last sweep" record — a sweep is cheap, idempotent and bounded from the
    jobs table — so every start sweeps after the startup delay, then settles
    into the interval, measured from the END of each sweep."""
    now = datetime(2026, 9, 20, 5, 22, tzinfo=UTC)
    reconciler = _reconciler(_World(), None, clock=lambda: now, interval_seconds=300, startup_delay_seconds=90)
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        nonlocal now
        if len(sleeps) == 3:
            raise asyncio.CancelledError
        sleeps.append(seconds)
        now += timedelta(seconds=seconds)

    async def _slow_sweep() -> dict[str, Any]:
        nonlocal now
        now += timedelta(seconds=40)
        return {"status": "ok", "dispatched": 0, "reopened": 0, "failed": 0, "deferred": 0}

    monkeypatch.setattr(reconciler, "run_once", _slow_sweep)
    monkeypatch.setattr("embry0.services.linear_reconciler.asyncio.sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await reconciler.start()

    assert sleeps == [90, 300, 300]


async def test_a_startup_delay_longer_than_the_interval_does_not_slow_the_first_sweep(monkeypatch) -> None:
    reconciler = _reconciler(_World(), None, interval_seconds=30, startup_delay_seconds=90)
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr("embry0.services.linear_reconciler.asyncio.sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        await reconciler.start()

    assert sleeps == [30]


async def test_an_unusable_clock_does_not_end_the_loop(monkeypatch) -> None:
    """The loop task is fire-and-forget: a raising clock (or any other
    scheduling failure) must fall back to the interval, not stop sweeping."""

    def _broken_clock() -> datetime:
        raise RuntimeError("no clock")

    reconciler = _reconciler(_World(), None, clock=_broken_clock, interval_seconds=300)
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr("embry0.services.linear_reconciler.asyncio.sleep", _sleep)
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await reconciler.start()

    assert sleeps == [300]
    assert [e["event"] for e in logs if e["log_level"] == "error"] == ["linear_reconcile_schedule_failed"]


# ---- the race: a webhook and a sweep reach the same ticket together --------


async def test_concurrent_webhook_and_sweep_dispatch_exactly_once() -> None:
    world = _World()
    ticket = _ticket("ENG-1")
    sync = _sync([ticket])
    sync.fetch_trigger_issue = AsyncMock(return_value=ticket)  # type: ignore[method-assign]
    payload = {"type": "Issue", "action": "update", "data": {"id": "uuid-ENG-1", "identifier": "ENG-1"}}

    results = await asyncio.gather(
        sync.handle_webhook_event(payload, _ctx(world, {"embry0"})),
        _reconciler(world, sync).run_once(),
        sync.handle_webhook_event(payload, _ctx(world, {"embry0"})),
    )

    assert len(world.jobs) == 1 and len(world.issues) == 1
    dispatched = [r for r in results if r.get("action") == "dispatched" or r.get("dispatched")]
    assert len(dispatched) == 1


async def test_concurrent_sweeps_and_relabel_retry_a_failed_ticket_exactly_once() -> None:
    world = _World()
    world.track("ENG-1", status="open", job_status="failed")
    ticket = _ticket("ENG-1")
    sync = _sync([ticket], reapplied=True)
    sync.fetch_trigger_issue = AsyncMock(return_value=ticket)  # type: ignore[method-assign]
    relabel = {
        "type": "Issue",
        "action": "update",
        "data": {
            "id": "uuid-ENG-1",
            "identifier": "ENG-1",
            "labelIds": ["lbl-e"],
            "labels": [{"id": "lbl-e", "name": "embry0"}],
        },
        "updatedFrom": {"labelIds": []},
    }

    await asyncio.gather(
        _reconciler(world, sync).run_once(),
        sync.handle_webhook_event(relabel, _ctx(world, {"embry0"})),
        _reconciler(world, sync).run_once(),
    )

    assert [j["status"] for j in world.jobs] == ["failed", "pending"]


# ---- review B2: shadow mode decides everything and changes nothing --------


def _events(logs: list[dict[str, Any]], prefix: str = "linear_reconcile_would") -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"].startswith(prefix)]


async def test_shadow_logs_what_it_would_do_and_writes_nothing() -> None:
    world, ledger = _World(), _Ledger()
    world.track("ENG-2", status="triaging", job_status="running")
    sync = _sync([_ticket("ENG-1"), _ticket("ENG-2"), _ticket("ENG-3", summary="")])
    reconciler = _reconciler(world, sync, ledger=ledger, mode="shadow")

    with capture_logs() as logs:
        result = await reconciler.run_once()

    assert result["mode"] == "shadow" and result["dispatched"] == [] and result["would_dispatch"] == ["ENG-1"]
    assert len(world.issues) == 1 and len(world.jobs) == 1  # only what track() put there
    sync.post_comment.assert_not_called()  # type: ignore[attr-defined]
    assert ledger.claimed == set() and ledger.failures == {}
    assert _events(logs) == [
        {
            "event": "linear_reconcile_would_dispatch",
            "log_level": "info",
            "identifier": "ENG-1",
            "repo": "o/r",
            "reason": "untracked",
            "after_reopen": False,
            "daily_dispatches": 0,
            "daily_cap": 5,
            "ticket_attempts": 0,
            "ticket_attempt_cap": 2,
        },
        {
            "event": "linear_reconcile_would_skip",
            "log_level": "info",
            "identifier": "ENG-2",
            "reason": "dedupe:live_job",
            "detail": "job `job-1` is still running for this ticket",
        },
        {
            "event": "linear_reconcile_would_skip",
            "log_level": "info",
            "identifier": "ENG-3",
            "reason": "unmapped_repo",
            "detail": "no repo: stamp on project 'Proj' and no LINEAR_REPO_MAP entry",
        },
    ]


async def test_shadow_logs_a_ticket_once_and_again_only_when_its_decision_changes() -> None:
    world = _World()
    issue_id = world.track("ENG-1", status="triaging", job_status="running")
    reconciler = _reconciler(world, _sync([_ticket("ENG-1")], reapplied=True), mode="shadow")

    with capture_logs() as logs:
        await reconciler.run_once()
        await reconciler.run_once()
        await reconciler.run_once()
    assert [e["reason"] for e in _events(logs)] == ["dedupe:live_job"]

    world.jobs[0]["status"] = "failed"
    world.issues[issue_id]["status"] = "open"
    with capture_logs() as logs:
        await reconciler.run_once()
        await reconciler.run_once()
    assert [(e["event"], e["reason"]) for e in _events(logs)] == [
        ("linear_reconcile_would_dispatch", "label_reapplied_after_failed_job")
    ]


async def test_shadow_forgets_a_ticket_that_left_the_listing() -> None:
    world, sync = _World(), _sync([_ticket("ENG-1")])
    reconciler = _reconciler(world, sync, mode="shadow")

    with capture_logs() as logs:
        await reconciler.run_once()
        sync.list_trigger_issues.return_value = []  # type: ignore[attr-defined]
        await reconciler.run_once()
        sync.list_trigger_issues.return_value = [_ticket("ENG-1")]  # type: ignore[attr-defined]
        await reconciler.run_once()

    assert len(_events(logs)) == 2


async def test_shadow_reports_the_caps_that_would_block() -> None:
    world = _World()
    for n in range(5):
        world.track(f"OLD-{n}", status="closed", job_status="completed", dispatched_by=ACTOR)
    reconciler = _reconciler(world, _sync([_ticket("ENG-1")]), mode="shadow")

    with capture_logs() as logs:
        await reconciler.run_once()

    (event,) = _events(logs)
    assert event["event"] == "linear_reconcile_would_skip" and event["reason"] == "reconciler_daily_cap"


# ---- review B2: durable spend bounds in `on` mode --------------------------


async def test_the_daily_cap_counts_reconciler_jobs_of_the_last_24h() -> None:
    world = _World()
    for n in range(4):
        world.track(f"OLD-{n}", status="closed", job_status="completed", dispatched_by=ACTOR)
    world.track("HUMAN-1", status="closed", job_status="completed", dispatched_by="alice")
    world.track("ANCIENT-1", status="closed", job_status="completed", dispatched_by=ACTOR)
    world.jobs[-1]["created_at"] = datetime.now(UTC) - timedelta(days=2)
    sync = _sync([_ticket("ENG-1"), _ticket("ENG-2")])

    result = await _reconciler(world, sync, max_dispatches_per_day=5).run_once()

    # 4 of 5 used: exactly one more starts, the other waits for tomorrow — silently.
    assert result["dispatched"] == ["ENG-1"]
    sync.post_comment.assert_awaited_once()  # type: ignore[attr-defined]  # the "dispatched" comment only


async def test_the_reconciler_starts_at_most_two_jobs_per_ticket_and_says_so_once() -> None:
    world, ledger = _World(), _Ledger()
    issue_id = world.track("ENG-1", status="open", job_status="failed", dispatched_by=ACTOR)
    world.jobs.append(
        {
            "job_id": "job-2",
            "issue_id": issue_id,
            "status": "failed",
            "finished_at": _FAILED_AT,
            "created_at": datetime.now(UTC) - timedelta(days=3),
            "dispatched_by": ACTOR,
        }
    )
    sync = _sync([_ticket("ENG-1")], reapplied=True)
    reconciler = _reconciler(world, sync, ledger=ledger)

    first = await reconciler.run_once()
    await reconciler.run_once()

    assert first["dispatched"] == [] and len(world.jobs) == 2
    assert ("linear", "ENG-1", "reconciler_ticket_attempts") in ledger.claimed
    assert sync.post_comment.await_count == 1  # type: ignore[attr-defined]
    assert "already started 2 job(s)" in sync.post_comment.await_args.args[1]  # type: ignore[attr-defined]


async def test_jobs_a_human_dispatched_do_not_count_against_the_ticket() -> None:
    world = _World()
    world.track("ENG-1", status="open", job_status="failed", dispatched_by="alice")
    world.track("ENG-1b", status="open", job_status="failed", dispatched_by="alice")

    result = await _reconciler(world, _sync([_ticket("ENG-1")], reapplied=True)).run_once()

    assert result["dispatched"] == ["ENG-1"]


# ---- review M2: a dispatch that keeps raising is backed off, then stopped --


async def test_failing_dispatches_back_off_exponentially_then_stop() -> None:
    world, ledger = _World(), _Ledger()
    world.execute_error = RuntimeError("sandbox backend down")
    sync = _sync([_ticket("ENG-1")])
    now = [datetime.now(UTC)]
    ledger.clock = lambda: now[0]
    reconciler = _reconciler(
        world, sync, ledger=ledger, clock=lambda: now[0], interval_seconds=300, failure_max_attempts=3
    )

    async def _sweep_after(seconds: int) -> int:
        now[0] += timedelta(seconds=seconds)
        before = ledger.failures.get("ENG-1", {}).get("failures", 0)
        await reconciler.run_once()
        return int(ledger.failures["ENG-1"]["failures"]) - int(before)

    assert await _sweep_after(0) == 1  # first try fails
    assert await _sweep_after(200) == 0  # inside the 300 s backoff: not even attempted
    assert await _sweep_after(200) == 1  # 400 s later: second try
    assert await _sweep_after(500) == 0  # backoff doubled to 600 s
    assert await _sweep_after(200) == 1  # third try
    assert await _sweep_after(86_400) == 0  # 3 failures = the cap: stopped for good
    assert await _sweep_after(86_400) == 0

    bodies = [call.args[1] for call in sync.post_comment.await_args_list]  # type: ignore[attr-defined]
    assert sum("Could not start work" in b for b in bodies) == 1
    assert sum("failed 3 times in a row" in b for b in bodies) == 1
    assert len(bodies) == 2 and world.jobs == []


async def test_a_successful_dispatch_resets_the_failure_backoff() -> None:
    world, ledger = _World(), _Ledger()
    world.execute_error = RuntimeError("boom")
    now = [datetime.now(UTC)]
    reconciler = _reconciler(world, _sync([_ticket("ENG-1")]), ledger=ledger, clock=lambda: now[0])

    await reconciler.run_once()
    world.execute_error = None
    now[0] += timedelta(hours=1)
    result = await reconciler.run_once()

    assert result["dispatched"] == ["ENG-1"] and ledger.failures == {}


# ---- review M3: a row stranded in `triaging` without a job -----------------


async def test_a_stale_triaging_row_is_reopened_and_dispatched_in_the_same_sweep() -> None:
    world = _World()
    issue_id = world.track(
        "ENG-1", status="triaging", job_status=None, updated_at=datetime.now(UTC) - timedelta(minutes=20)
    )

    with capture_logs() as logs:
        result = await _reconciler(world, _sync([_ticket("ENG-1")])).run_once()

    assert result["reopened"] == ["ENG-1"] and result["dispatched"] == ["ENG-1"]
    assert world.jobs[0]["issue_id"] == issue_id
    assert any(e["event"] == "linear_reconcile_reopened_stale_triaging" for e in logs)


async def test_a_fresh_or_live_triaging_row_is_left_alone() -> None:
    world = _World()
    fresh = world.track("ENG-1", status="triaging", job_status=None)
    live = world.track(
        "ENG-2", status="triaging", job_status="running", updated_at=datetime.now(UTC) - timedelta(hours=2)
    )

    result = await _reconciler(world, _sync([])).run_once()

    assert result["reopened"] == []
    assert world.issues[fresh]["status"] == world.issues[live]["status"] == "triaging"


async def test_shadow_reports_a_stale_triaging_row_without_reopening_it() -> None:
    world = _World()
    issue_id = world.track(
        "ENG-1", status="triaging", job_status=None, updated_at=datetime.now(UTC) - timedelta(minutes=20)
    )
    reconciler = _reconciler(world, _sync([]), mode="shadow")

    with capture_logs() as logs:
        await reconciler.run_once()
        await reconciler.run_once()

    assert world.issues[issue_id]["status"] == "triaging"
    assert _events(logs) == [
        {
            "event": "linear_reconcile_would_reopen",
            "log_level": "info",
            "identifier": "ENG-1",
            "issue_id": issue_id,
            "reason": "stale_triaging",
            "older_than_minutes": 15,
        }
    ]


# ---- review nits 2/3: shadow says what `on` would do, and says it once ------


async def test_shadow_reports_reopen_then_dispatch_for_a_stale_triaging_ticket() -> None:
    world = _World()
    issue_id = world.track(
        "ENG-1", status="triaging", job_status=None, updated_at=datetime.now(UTC) - timedelta(minutes=20)
    )
    reconciler = _reconciler(world, _sync([_ticket("ENG-1")]), mode="shadow")

    with capture_logs() as logs:
        first = await reconciler.run_once()
        second = await reconciler.run_once()

    # What `on` does with this ticket: reopen it, then dispatch it. Shadow says exactly that.
    assert [(e["event"], e.get("after_reopen")) for e in _events(logs)] == [
        ("linear_reconcile_would_reopen", None),
        ("linear_reconcile_would_dispatch", True),
    ]
    assert world.issues[issue_id]["status"] == "triaging" and world.jobs == []
    for result in (first, second):
        assert result["would_reopen"] == ["ENG-1"] and result["reopened"] == []
        assert result["would_dispatch"] == ["ENG-1"]


async def test_a_lingering_stale_row_does_not_log_a_sweep_line_every_interval(monkeypatch) -> None:
    world = _World()
    world.track("ENG-1", status="triaging", job_status=None, updated_at=datetime.now(UTC) - timedelta(minutes=20))
    reconciler = _reconciler(world, _sync([_ticket("ENG-1")]), mode="shadow", interval_seconds=1)
    sweeps = 0
    real_run_once = reconciler.run_once

    async def _three_sweeps() -> dict[str, Any]:
        nonlocal sweeps
        sweeps += 1
        if sweeps > 3:
            raise asyncio.CancelledError
        return await real_run_once()

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(reconciler, "run_once", _three_sweeps)
    monkeypatch.setattr("embry0.services.linear_reconciler.asyncio.sleep", _no_sleep)

    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await reconciler.start()

    assert [e for e in logs if e["event"] == "linear_reconcile_sweep"] == []
    assert len(_events(logs)) == 2  # would_reopen + would_dispatch, once each across three sweeps


async def test_on_mode_names_the_field_reopened() -> None:
    world = _World()
    world.track("ENG-1", status="triaging", job_status=None, updated_at=datetime.now(UTC) - timedelta(minutes=20))

    result = await _reconciler(world, _sync([])).run_once()

    assert result["reopened"] == ["ENG-1"] and result["would_reopen"] == []
