"""The database-backed producers and sources, against real Postgres (#285)."""

from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import CollectorRegistry

from embry0.metrics import LoopMetrics
from embry0.services.healing.lifecycle import Origin, ResolutionPath
from embry0.services.healing.service import HealingCaseService, TrackerRef
from embry0.storage.database import DatabasePool
from embry0.storage.repositories.healing_cases import HealingCasesRepository
from embry0.storage.repositories.jobs import JobsRepository
from embry0.storage.repositories.traces import TracesRepository
from embry0.storage.repositories.watcher_runs import WatcherRunsRepository

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

REPO = "acme/widgets"


@pytest.fixture
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture
def metrics(registry: CollectorRegistry) -> LoopMetrics:
    return LoopMetrics(registry)


# ---- jobs ----------------------------------------------------------------------


async def test_a_terminal_transition_is_counted_once_with_its_pipeline_and_code(
    db_with_migrations: DatabasePool, metrics: LoopMetrics, registry: CollectorRegistry
) -> None:
    jobs = JobsRepository(db_with_migrations, metrics=metrics)
    job_id = await jobs.create(repo=REPO, task="fix", pipeline_template="issue-to-pr")
    started = datetime.now(UTC) - timedelta(minutes=5)
    await jobs.update(job_id, status="running", started_at=started)
    labels = {"pipeline_template": "issue-to-pr", "status": "failed", "error_code": "ERR_MAX_RETRIES"}
    assert (
        registry.get_sample_value("embry0_jobs_finished_total", {**labels, "error_category": "uncategorised"}) is None
    )

    await jobs.update(job_id, status="failed", error_code="ERR_MAX_RETRIES", error_message="job-1 /app/x.py boom")
    await jobs.update(job_id, status="failed")  # same-status no-op: not a second outcome

    assert registry.get_sample_value("embry0_jobs_finished_total", {**labels, "error_category": "uncategorised"}) == 1
    duration = {"pipeline_template": "issue-to-pr", "status": "failed"}
    assert registry.get_sample_value("embry0_job_duration_seconds_count", duration) == 1
    assert registry.get_sample_value("embry0_job_duration_seconds_sum", duration) >= 299


async def test_a_non_terminal_transition_is_not_an_outcome(
    db_with_migrations: DatabasePool, metrics: LoopMetrics, registry: CollectorRegistry
) -> None:
    jobs = JobsRepository(db_with_migrations, metrics=metrics)
    job_id = await jobs.create(repo=REPO, task="fix")
    await jobs.update(job_id, status="running")
    await jobs.update(job_id, status="awaiting_input")
    assert not [s for family in registry.collect() if family.name == "embry0_jobs_finished" for s in family.samples]


async def test_boot_time_orphans_are_counted(
    db_with_migrations: DatabasePool, metrics: LoopMetrics, registry: CollectorRegistry
) -> None:
    jobs = JobsRepository(db_with_migrations, metrics=metrics)
    job_id = await jobs.create(repo=REPO, task="fix", pipeline_template="analysis-default")
    await jobs.update(job_id, status="running")

    assert job_id in await jobs.fail_orphaned(
        "ERR_ORPHANED", "Orchestrator restarted", never_started_message="Never started"
    )

    labels = {
        "pipeline_template": "analysis-default",
        "status": "failed",
        "error_code": "ERR_ORPHANED",
        "error_category": "uncategorised",
    }
    assert registry.get_sample_value("embry0_jobs_finished_total", labels) >= 1


async def test_a_repository_without_metrics_behaves_as_before(db_with_migrations: DatabasePool) -> None:
    jobs = JobsRepository(db_with_migrations)
    job_id = await jobs.create(repo=REPO, task="fix")
    await jobs.update(job_id, status="cancelled")
    assert (await jobs.get(job_id))["status"] == "cancelled"  # type: ignore[index]


async def test_spend_is_counted_per_trace_under_the_jobs_pipeline(
    db_with_migrations: DatabasePool, metrics: LoopMetrics, registry: CollectorRegistry
) -> None:
    jobs = JobsRepository(db_with_migrations)
    traces = TracesRepository(db_with_migrations, metrics=metrics)
    routed = await jobs.create(repo=REPO, task="fix", pipeline_template="analysis-default")
    unrouted = await jobs.create(repo=REPO, task="fix")

    await traces.create(job_id=routed, agent_type="analysis", model="claude-opus-5", result="success", cost_usd=1.5)
    await traces.create(job_id=unrouted, agent_type="triage", model="grok-4.5", result="success", cost_usd=0.25)
    await traces.create(job_id=routed, agent_type="analysis", model="claude-opus-5", result="error", cost_usd=0.0)

    spend = "embry0_agent_spend_usd_total"
    assert (
        registry.get_sample_value(
            spend, {"pipeline_template": "analysis-default", "provider": "anthropic", "model_family": "claude-opus"}
        )
        == 1.5
    )
    assert (
        registry.get_sample_value(spend, {"pipeline_template": "unassigned", "provider": "xai", "model_family": "grok"})
        == 0.25
    )
    assert await traces.total_cost_for_job(routed) == 1.5  # the rows themselves are unchanged


# ---- watcher_runs ----------------------------------------------------------------


async def test_last_tick_times_tell_any_tick_from_an_analysed_one(db_with_migrations: DatabasePool) -> None:
    runs = WatcherRunsRepository(db_with_migrations)
    await db_with_migrations.execute("DELETE FROM watcher_runs")
    assert await runs.last_tick_times() == {"last_tick_at": None, "last_analysed_at": None, "last_verdict_at": None}

    now = datetime.now(UTC)
    window = {"window_start": now - timedelta(hours=1), "window_end": now}
    analysed = await runs.create(action="no_issue", **window)
    errored = await runs.create(action="error", detail="loki down", **window)
    await db_with_migrations.execute(
        "UPDATE watcher_runs SET created_at = $2 WHERE id = $1", analysed, now - timedelta(hours=9)
    )
    await db_with_migrations.execute(
        "UPDATE watcher_runs SET created_at = $2 WHERE id = $1", errored, now - timedelta(minutes=5)
    )

    times = await runs.last_tick_times()
    assert abs((times["last_tick_at"] - (now - timedelta(minutes=5))).total_seconds()) < 1  # type: ignore[operator]
    assert abs((times["last_analysed_at"] - (now - timedelta(hours=9))).total_seconds()) < 1  # type: ignore[operator]


async def test_last_tick_times_count_a_quiet_tick_as_a_verdict_but_not_a_window(
    db_with_migrations: DatabasePool,
) -> None:
    # 2026-09-27: a restart after a quiet night must seed the gauge from the
    # quiet tick, or Embry0WatcherNoVerdict3h re-arms on every deploy — while
    # the next query window still starts where the last ANALYSED one ended.
    runs = WatcherRunsRepository(db_with_migrations)
    await db_with_migrations.execute("DELETE FROM watcher_runs")

    now = datetime.now(UTC)
    analysed_end = now - timedelta(hours=9)
    analysed = await runs.create(
        action="no_issue", window_start=analysed_end - timedelta(hours=1), window_end=analysed_end
    )
    quiet = await runs.create(action="skipped_quiet", window_start=analysed_end, window_end=now)
    await db_with_migrations.execute("UPDATE watcher_runs SET created_at = $2 WHERE id = $1", analysed, analysed_end)
    await db_with_migrations.execute(
        "UPDATE watcher_runs SET created_at = $2 WHERE id = $1", quiet, now - timedelta(minutes=5)
    )

    times = await runs.last_tick_times()
    assert abs((times["last_verdict_at"] - (now - timedelta(minutes=5))).total_seconds()) < 1, (  # type: ignore[operator]
        "the quiet tick must seed the verdict timestamp"
    )
    assert abs((times["last_analysed_at"] - analysed_end).total_seconds()) < 1, (  # type: ignore[operator]
        "but not the analysed one"
    )
    assert await runs.last_analysed_window_end() == analysed_end, "and it must not consume the window"


# ---- healing cases -----------------------------------------------------------------


async def _case(service: HealingCaseService, key: str, origin: Origin = Origin.WATCHER) -> str:
    tracker = TrackerRef(kind="linear", key=key, url=f"https://linear.app/x/issue/{key}")
    return (await service.open_case(repo=REPO, origin=origin, title="boom", tracker=tracker))["id"]


async def test_metrics_snapshot_aggregates_the_ledger(db_with_migrations: DatabasePool) -> None:
    await db_with_migrations.execute("DELETE FROM healing_cases")
    repo = HealingCasesRepository(db_with_migrations)
    service = HealingCaseService(repo)
    now = datetime.now(UTC)

    waiting = await _case(service, "ENG-1")
    await _case(service, "ENG-2")
    await _case(service, "ENG-3", Origin.HUMAN)
    await db_with_migrations.execute(
        "UPDATE healing_cases SET tracked_at = $2 WHERE id = $1", waiting, now - timedelta(days=3)
    )
    fixed = await _case(service, "ENG-4")
    merged_at = now - timedelta(hours=2)
    await service.record_merge(fixed, merge_sha="abc123", resolution_path=ResolutionPath.OUT_OF_BAND, at=merged_at)
    await service.record_deploy(fixed, sha="def456", marker_at=now - timedelta(hours=1), at=now - timedelta(hours=1))
    gone = await _case(service, "ENG-5")
    await service.close(gone, resolution_path=ResolutionPath.CANCELLED)

    snapshot = await service.metrics_snapshot()

    open_counts = {(r["stage"], r["origin"]): r["count"] for r in snapshot["open"]}
    assert open_counts == {("tracked", "watcher"): 2, ("tracked", "human"): 1, ("deployed", "watcher"): 1}
    assert abs(snapshot["oldest_age_seconds"]["tracked"] - 3 * 86400) < 60
    assert abs(snapshot["oldest_age_seconds"]["deployed"] - 3600) < 60
    assert snapshot["awaiting_verdict"] == 1
    assert snapshot["closed"] == {"cancelled": 1}


async def test_case_events_are_counted_after_commit(
    db_with_migrations: DatabasePool, metrics: LoopMetrics, registry: CollectorRegistry
) -> None:
    service = HealingCaseService(HealingCasesRepository(db_with_migrations), metrics=metrics)
    case_id = await _case(service, "ENG-10")
    await service.record_merge(case_id, merge_sha="abc", resolution_path=ResolutionPath.OUT_OF_BAND)
    await service.close(case_id)

    get = registry.get_sample_value
    assert get("embry0_healing_cases_opened_total", {"origin": "watcher"}) == 1
    assert get("embry0_healing_case_events_total", {"event": "merged", "effect": "applied"}) == 1
    assert get("embry0_healing_case_events_total", {"event": "closed", "effect": "applied"}) == 1
    # The bypass rate: this case was fixed by a human outside embry0.
    assert get("embry0_healing_cases_resolved_total", {"resolution_path": "out_of_band"}) == 1
    assert get("embry0_healing_cases_resolved_total", {"resolution_path": "embry0"}) is None


async def test_a_write_that_rolls_back_is_not_counted(
    db_with_migrations: DatabasePool, metrics: LoopMetrics, registry: CollectorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from embry0.storage.repositories.healing_cases import LockedCase

    service = HealingCaseService(HealingCasesRepository(db_with_migrations), metrics=metrics)
    case_id = await _case(service, "ENG-11")
    real_add_event = LockedCase.add_event

    async def add_then_fail(self: LockedCase, *args: object, **kwargs: object) -> None:
        await real_add_event(self, *args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("connection lost before commit")

    monkeypatch.setattr(LockedCase, "add_event", add_then_fail)
    with pytest.raises(RuntimeError):
        await service.record_merge(case_id, merge_sha="abc", resolution_path=ResolutionPath.EMBRY0)
    monkeypatch.undo()

    assert (await service.get(case_id))["stage"] == "tracked"  # type: ignore[index]
    assert (
        registry.get_sample_value("embry0_healing_case_events_total", {"event": "merged", "effect": "applied"}) is None
    )
