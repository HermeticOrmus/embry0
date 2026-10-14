"""#451: the confirm sweep reaches ``HealingCaseService.reopen`` with the real
repository and service — the objects production wires (`embry0/api/app.py`).

``reopen()`` existed and was tested, yet nothing in production called it, so
a ``not_healed`` case sat at ``verified`` looking exactly like one whose fix
had worked. This pins the whole path against Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry

from embry0.metrics import LoopMetrics
from embry0.metrics.snapshot import cases_source
from embry0.services.healing.confirm_sweep import MAX_NOT_HEALED_RETRIES, ConfirmHealedSweep
from embry0.services.healing.lifecycle import Origin, ResolutionPath, Stage
from embry0.services.healing.recorder import CaseRecorder
from embry0.services.healing.service import HealingCaseService, TrackerRef
from embry0.storage.repositories.healing_cases import HealingCasesRepository
from embry0.storage.repositories.log_signatures import SignatureRow

from .conftest import REPO, Seed

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]


def _still_firing() -> SignatureRow:
    now = datetime.now(UTC)
    return SignatureRow(
        signature_id="sig1",
        repo=REPO,
        template="template",
        first_seen_at=now - timedelta(hours=48),
        last_seen_at=now - timedelta(minutes=5),
        occurrence_count=400,
        ticks_seen=40,
        last_window_count=7,
        services=["backend"],
        sample_lines=["a line"],
    )


async def _ship_a_fix(service: HealingCaseService, case_id: str, sha: str, hours_ago: int) -> None:
    now = datetime.now(UTC)
    await service.record_merge(
        case_id, merge_sha=f"m-{sha}", resolution_path=ResolutionPath.EMBRY0, at=now - timedelta(hours=hours_ago + 1)
    )
    await service.record_deploy(case_id, sha=sha, marker_at=now - timedelta(hours=hours_ago))


async def test_production_reaches_reopen_then_stops_at_the_budget(
    cases_repo: HealingCasesRepository, service: HealingCaseService
) -> None:
    assert MAX_NOT_HEALED_RETRIES == 1, "this test walks a budget of exactly one retry"
    case_id = (await service.open_case(repo=REPO, title="boom", origin=Origin.WATCHER, signature_id="sig1"))["id"]
    await _ship_a_fix(service, case_id, "d1", hours_ago=6)

    sweep = ConfirmHealedSweep(
        cases_repo=cases_repo,
        cases_service=service,
        signatures_repo=SimpleNamespace(get=_returns(_still_firing())),
        config=SimpleNamespace(watcher_repo=REPO),
    )
    tally = await sweep.run_once()
    assert tally["not_healed"] == 1, tally
    case = await service.get(case_id)
    assert case is not None
    assert case["stage"] == Stage.TRACKED, (
        f"a not_healed case with a retry left is at {case['stage']!r}, not back with a person: "
        "production never reached reopen(), or reopen parked it elsewhere"
    )
    assert tally["reopened"] == 1, tally

    # A pass over unchanged state reopens nothing further.
    assert (await sweep.run_once())["reopened"] == 0


async def test_a_second_failed_fix_spends_the_budget_and_stays_put(
    cases_repo: HealingCasesRepository, service: HealingCaseService
) -> None:
    from embry0.services.healing.lifecycle import Verdict

    assert MAX_NOT_HEALED_RETRIES == 1, "this test walks a budget of exactly one retry"
    case_id = (await service.open_case(repo=REPO, title="boom", origin=Origin.WATCHER, signature_id="sig1"))["id"]
    # Attempt 1 failed and was reopened a day ago; the retry shipped since.
    await _ship_a_fix(service, case_id, "d1", hours_ago=30)
    await service.record_verdict(case_id, Verdict.NOT_HEALED, evidence={})
    await service.reopen(case_id, reason="retry 1 of 1", at=datetime.now(UTC) - timedelta(hours=28))
    await _ship_a_fix(service, case_id, "d2", hours_ago=5)

    sweep = ConfirmHealedSweep(
        cases_repo=cases_repo,
        cases_service=service,
        signatures_repo=SimpleNamespace(get=_returns(_still_firing())),
        config=SimpleNamespace(watcher_repo=REPO),
    )
    tally = await sweep.run_once()
    assert (tally["not_healed"], tally["reopened"]) == (1, 0), tally
    case = await service.get(case_id)
    assert case is not None
    assert case["stage"] == Stage.VERIFIED, f"a case past its retry budget moved to {case['stage']!r}"
    assert case["verdict_evidence"]["retries"] == {"used": 1, "allowed": 1, "next": "a person decides"}
    assert [e["event"] for e in case["events"]].count("reopened") == 1


async def test_a_not_healed_verdict_recorded_before_the_wiring_is_still_retried(
    cases_repo: HealingCasesRepository, service: HealingCaseService
) -> None:
    """The live shape of #451: a verdict already on record, no reopen ever made."""
    from embry0.services.healing.lifecycle import Verdict

    case_id = (await service.open_case(repo=REPO, title="boom", origin=Origin.WATCHER))["id"]
    await _ship_a_fix(service, case_id, "d1", hours_ago=6)
    await service.record_verdict(case_id, Verdict.NOT_HEALED, evidence={"seen_since_deploy": True})

    sweep = ConfirmHealedSweep(
        cases_repo=cases_repo,
        cases_service=service,
        signatures_repo=SimpleNamespace(get=_returns(None)),
        config=SimpleNamespace(watcher_repo=REPO),
    )
    assert (await sweep.run_once())["reopened"] == 1
    case = await service.get(case_id)
    assert case is not None and case["stage"] == Stage.TRACKED, case and case["stage"]


async def test_a_reopened_case_waits_for_a_person_and_accepting_it_dispatches_the_retry(
    cases_repo: HealingCasesRepository, service: HealingCaseService, seed: Seed
) -> None:
    """#658, in the shape production has: a watcher draft with a ticket,
    accepted by its job (which released the draft's slot) weeks ago, fixed,
    deployed, then judged ``not_healed`` by the sweep.

    The reopen dispatches nothing, so the case must wait for a person: out of
    the accepted-age gauge the dispatch alert reads, listed as awaiting a
    decision, holding no draft slot. Accepting it again dispatches a job on
    the same issue, and every dispatch links its job to the case the way
    ``CaseRecorder.job_dispatched`` does here; that is what accepts it, aged
    from that moment.
    """
    now = datetime.now(UTC)
    ticket = TrackerRef("linear", "ENG-7", "https://linear.app/acme/issue/ENG-7")
    run_id = await seed.watcher_run("ENG-7")
    case_id = (
        await service.open_case(
            repo=REPO,
            origin=Origin.WATCHER,
            title="boom",
            signature_id="sig1",
            watcher_run_id=run_id,
            tracker=ticket,
            at=now - timedelta(days=21),
        )
    )["id"]
    issue_id = await seed.issue()
    issue = {"id": issue_id, "repo": REPO, "tracker": "linear", "tracker_key": "ENG-7", "title": "boom"}
    first_job = await seed.job(issue_id)
    await service.attach_job(case_id, first_job, at=now - timedelta(days=20), event_key=f"job:{first_job}")
    await _ship_a_fix(service, case_id, "d1", hours_ago=6)

    registry = CollectorRegistry()
    gauges = cases_source(LoopMetrics(registry), service.metrics_snapshot)

    async def accepted_age() -> float:
        gauges.apply(await gauges.fetch())
        value = registry.get_sample_value("embry0_healing_oldest_open_case_age_seconds", {"stage": "accepted"})
        assert value is not None
        return value

    sweep = ConfirmHealedSweep(
        cases_repo=cases_repo,
        cases_service=service,
        signatures_repo=SimpleNamespace(get=_returns(_still_firing())),
        config=SimpleNamespace(watcher_repo=REPO),
    )
    assert (await sweep.run_once())["reopened"] == 1

    case = await service.get(case_id)
    assert case is not None
    assert case["stage"] == Stage.TRACKED, f"a reopened case is at {case['stage']!r}: nothing will dispatch it there"
    assert await accepted_age() == 0, "a reopened case is counted by the accepted-age gauge the dispatch alert reads"
    waiting, _total = await cases_repo.list_page(awaiting_decision=True)
    assert case_id in {row["id"] for row in waiting}, "a reopened case is not listed as awaiting a decision"
    assert await cases_repo.count_open_unaccepted_watcher_cases(days=30, repo=REPO) == 0, (
        "a reopened case took a draft slot: its draft was answered once"
    )

    # A person accepts the retry: its job is linked to the case.
    retry_job = await seed.job(issue_id)
    assert await CaseRecorder(service).job_dispatched(issue, retry_job) == case_id
    case = await service.get(case_id)
    assert case is not None
    assert case["stage"] == Stage.ACCEPTED, case["stage"]
    assert [job["job_id"] for job in case["jobs"]] == [first_job, retry_job]
    assert await accepted_age() < 60, "the re-acceptance must be aged from itself, not from the first acceptance"
    waiting, _total = await cases_repo.list_page(awaiting_decision=True)
    assert case_id not in {row["id"] for row in waiting}


def _returns(value):
    async def _call(*_a, **_k):
        return value

    return _call
