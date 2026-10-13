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

from embry0.services.healing.confirm_sweep import MAX_NOT_HEALED_RETRIES, ConfirmHealedSweep
from embry0.services.healing.lifecycle import Origin, ResolutionPath, Stage
from embry0.services.healing.service import HealingCaseService
from embry0.storage.repositories.healing_cases import HealingCasesRepository
from embry0.storage.repositories.log_signatures import SignatureRow

from .conftest import REPO

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
    assert case["stage"] == Stage.ACCEPTED, (
        f"a not_healed case with a retry left stayed at {case['stage']!r}: production never reached reopen()"
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
    assert case is not None and case["stage"] == Stage.ACCEPTED, case and case["stage"]


def _returns(value):
    async def _call(*_a, **_k):
        return value

    return _call
