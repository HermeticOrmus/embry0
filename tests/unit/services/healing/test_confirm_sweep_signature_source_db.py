"""#657: the confirm sweep reads how a case's signature was chosen, from the
real repository — a fallback-anchored case gets ``inconclusive``, not a verdict
on a line its evidence never quoted, and is never reopened for a retry.

Against Postgres on purpose: the guard is only as good as ``awaiting_verdict``
selecting the column. A query that left it out would make every case look
unrecorded, and the guard would quietly never fire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from embry0.services.healing.confirm_sweep import ConfirmHealedSweep
from embry0.services.healing.lifecycle import Origin, ResolutionPath, SignatureSource, Stage, Verdict
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


async def _deployed_case(service: HealingCaseService, source: SignatureSource) -> str:
    case_id = (
        await service.open_case(
            repo=REPO, title="boom", origin=Origin.WATCHER, signature_id="sig1", signature_source=source
        )
    )["id"]
    now = datetime.now(UTC)
    await service.record_merge(
        case_id, merge_sha="m-1", resolution_path=ResolutionPath.EMBRY0, at=now - timedelta(hours=7)
    )
    await service.record_deploy(case_id, sha="d1", marker_at=now - timedelta(hours=6))
    return case_id


def _sweep(cases_repo: HealingCasesRepository, service: HealingCaseService, comments: list) -> ConfirmHealedSweep:
    async def _get(*_a, **_k):
        return _still_firing()

    async def _comment(key, body):
        comments.append((key, body))

    return ConfirmHealedSweep(
        cases_repo=cases_repo,
        cases_service=service,
        signatures_repo=SimpleNamespace(get=_get),
        config=SimpleNamespace(watcher_repo=REPO),
        commenter=_comment,
    )


async def test_a_fallback_case_is_inconclusive_and_not_sent_back(
    cases_repo: HealingCasesRepository, service: HealingCaseService
) -> None:
    case_id = await _deployed_case(service, SignatureSource.FALLBACK)
    comments: list = []

    tally = await _sweep(cases_repo, service, comments).run_once()

    case = await service.get(case_id)
    assert case is not None
    assert case["healed_verdict"] == Verdict.INCONCLUSIVE, (
        f"a fallback-anchored case got {case['healed_verdict']!r} from a line its evidence never quoted"
    )
    assert case["verdict_evidence"]["signature_source"] == "fallback"
    assert (tally["not_healed"], tally["reopened"]) == (0, 0), tally
    assert case["stage"] == Stage.VERIFIED
    assert comments == [], "an inconclusive verdict is not posted on the ticket"


async def test_an_evidence_case_is_still_judged(
    cases_repo: HealingCasesRepository, service: HealingCaseService
) -> None:
    """The control: the guard must not swallow the verdicts it is not about."""
    case_id = await _deployed_case(service, SignatureSource.EVIDENCE)

    tally = await _sweep(cases_repo, service, []).run_once()

    assert (tally["not_healed"], tally["reopened"]) == (1, 1), tally
    case = await service.get(case_id)
    assert case is not None
    [verdict] = [e for e in case["events"] if e["event"] == "verdict_recorded"]
    evidence = verdict["detail"]["updates"]["verdict_evidence"]
    assert evidence["signature_source"] == "evidence", evidence


async def test_open_case_refuses_a_source_outside_the_vocabulary(service: HealingCaseService) -> None:
    with pytest.raises(ValueError):
        await service.open_case(repo=REPO, title="boom", origin=Origin.WATCHER, signature_source="loudest")
