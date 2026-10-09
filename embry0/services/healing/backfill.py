"""Backfill: one healing case per historical watcher proposal (#278).

Rebuilds what the ledger would have recorded had it existed, from the three
tables that did: ``watcher_runs`` (action='proposed') → the issue carrying
the proposal's Linear identifier → that issue's jobs → their ``pr_url``.

It records only what those rows prove, and says so where they fall short:

* A proposal whose Linear id reconcile erased (pre-#278) gets a case with NO
  tracker — the id is not guessed from titles. Reconcile only ever erased
  the id of a draft it had *released* (cancelled, completed or deleted in
  Linear), so that case is known dead: it is closed as ``cancelled`` with
  ``LOST_TRACKER_NOTE`` and can never pin the proposal cap.
* Each distinct ``pr_url`` becomes a PR of the case. A job in ``pr_merged``
  proves a merge (the case is ``merged``, with an empty ``merge_sha``: the
  commit was never stored); a job in ``pr_closed`` proves the PR was closed
  unmerged. A PR with neither is left OPEN and the case carries
  ``UNVERIFIED_PR_NOTE`` — on 2026-09-20 three such PRs were in fact merged
  on GitHub behind ``failed`` jobs, which the database cannot show. Merge
  sync (#279) records those; the backfill never guesses.
* A draft that still has its id but never became an issue stays ``tracked``.
  Whether Linear has since closed it is not in the database.
* No verdict is set.

Every write goes through ``HealingCaseService``, so a second pass is a
no-op and a pass after the pipeline has moved on only adds what is new. A
case that is closed or has ever had a verdict is never touched again. Each
proposal is ONE transaction: a pass cut short (the startup timeout) leaves
whole cases behind, never a half-built one.
Runs at orchestrator startup. The live writers (``recorder.CaseRecorder``,
part 2 of #278) open a case the moment a proposal is filed, so on a running
system this pass finds nothing to do; it remains the repair for a proposal
whose fail-open ledger write was lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog

from embry0.services.healing.lifecycle import Origin, ResolutionPath, Stage
from embry0.services.healing.service import HealingCaseService, TrackerRef
from embry0.storage.repositories.healing_cases import HealingCasesRepository

logger = structlog.get_logger(__name__)

LOST_TRACKER_NOTE = (
    "Backfilled without a tracker: watcher reconcile erased this proposal's Linear id before #278. "
    "It only did that to drafts it had released (cancelled, completed or deleted in Linear), so the "
    "case is closed as cancelled. The id was not guessed."
)
UNVERIFIED_PR_NOTE = (
    "Backfilled with a PR whose outcome the database cannot prove: no job for it reached pr_merged or "
    "pr_closed, so it is recorded as open. It may have been merged or closed on the forge; merge sync "
    "(#279) will record that. Nothing was guessed."
)

_PR_NUMBER = re.compile(r"/(?:pull|merge_requests)/(\d+)/?$")
_PROOF = ("pr_merged", "pr_closed")


@dataclass
class BackfillReport:
    proposals: int = 0
    created: int = 0
    existing: int = 0
    without_tracker: int = 0
    unverified_prs: int = 0
    skipped_no_repo: int = 0


@dataclass(frozen=True)
class _HistoricalPr:
    url: str
    first_job: dict[str, Any]
    # The job whose status proves the PR's outcome, or None when none does.
    proof: dict[str, Any] | None
    last_job: dict[str, Any]


def _pr_number(pr_url: str) -> int | None:
    found = _PR_NUMBER.search(pr_url)
    return int(found.group(1)) if found else None


def _historical_prs(jobs: list[dict[str, Any]]) -> list[_HistoricalPr]:
    """One entry per distinct pr_url, in the order the PRs first appeared."""
    by_url: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        if job["pr_url"]:
            by_url.setdefault(job["pr_url"], []).append(job)
    prs = []
    for url, group in by_url.items():
        proofs = [j for status in _PROOF for j in group if j["status"] == status]
        prs.append(_HistoricalPr(url, group[0], proofs[0] if proofs else None, group[-1]))
    return prs


def _settled(case: dict[str, Any]) -> bool:
    """Closed, or judged at least once. From its first verdict on a case can
    be reopened for a retry, and replaying the first attempt's PR and merge
    from the jobs table would then overwrite the retry's state."""
    return case["stage"] == Stage.CLOSED or case["verified_at"] is not None


async def backfill_watcher_proposals(repo: HealingCasesRepository, *, default_repo: str) -> BackfillReport:
    """``default_repo`` is WATCHER_REPO: analysis jobs carry no repo, so it is
    the only source for a proposal that never became an issue."""
    report = BackfillReport()
    for chain in await repo.watcher_proposal_chains():
        report.proposals += 1
        if not (chain["issue_repo"] or default_repo):
            report.skipped_no_repo += 1
            logger.warning("healing_backfill_no_repo", watcher_run_id=chain["watcher_run_id"])
            continue
        async with repo.atomic() as bound:
            outcome = await _backfill_one(bound, HealingCaseService(bound), chain, default_repo)
        # Counted only once the case's transaction has committed.
        report.existing += outcome.existing
        report.created += not outcome.existing
        report.without_tracker += outcome.without_tracker
        report.unverified_prs += outcome.unverified_prs

    logger.info(
        "healing_backfill_done",
        proposals=report.proposals,
        created=report.created,
        existing=report.existing,
        without_tracker=report.without_tracker,
        unverified_prs=report.unverified_prs,
        skipped_no_repo=report.skipped_no_repo,
    )
    return report


@dataclass(frozen=True)
class _CaseOutcome:
    existing: bool
    without_tracker: bool = False
    unverified_prs: int = 0


async def _backfill_one(
    repo: HealingCasesRepository, service: HealingCaseService, chain: dict[str, Any], default_repo: str
) -> _CaseOutcome:
    run_id = chain["watcher_run_id"]
    existing = await repo.find_by_watcher_run(run_id)
    if existing is not None and _settled(existing):
        return _CaseOutcome(existing=True)

    identifier = chain["tracker_key"]
    prs = _historical_prs(chain["jobs"])
    unverified = sum(1 for pr in prs if pr.proof is None)
    note = None if identifier else LOST_TRACKER_NOTE
    if unverified:
        note = UNVERIFIED_PR_NOTE
    case = existing or await service.open_case(
        repo=chain["issue_repo"] or default_repo,
        origin=Origin.WATCHER,
        title=chain["detail"] or chain["issue_title"] or "(untitled watcher proposal)",
        fingerprint=chain["fingerprint"],
        watcher_run_id=run_id,
        tracker=TrackerRef(chain["tracker"] or "linear", identifier, chain["issue_tracker_url"])
        if identifier
        else None,
        note=note,
        at=chain["proposed_at"],
    )
    await _replay_pipeline(service, case["id"], chain, prs)
    if identifier is None:
        await service.close(case["id"], resolution_path=ResolutionPath.CANCELLED)
    if existing is not None:
        return _CaseOutcome(existing=True)
    return _CaseOutcome(existing=False, without_tracker=identifier is None, unverified_prs=unverified)


async def _replay_pipeline(
    service: HealingCaseService, case_id: str, chain: dict[str, Any], prs: list[_HistoricalPr]
) -> None:
    if chain["issue_id"] is None:
        return
    await service.attach_issue(case_id, chain["issue_id"], at=chain["issue_created_at"])
    for job in chain["jobs"]:
        await service.attach_job(case_id, job["job_id"], at=job["created_at"])

    for pr in prs:
        credited = pr.proof or pr.last_job
        await service.attach_pr(
            case_id,
            url=pr.url,
            number=_pr_number(pr.url),
            job_id=credited["job_id"],
            at=pr.first_job["finished_at"] or pr.first_job["created_at"],
        )
        if pr.proof is None:
            continue
        # updated_at is when the webhook flipped the job row: the closest
        # record of when the PR was merged or closed.
        flipped_at = pr.proof["updated_at"] or pr.proof["finished_at"]
        if pr.proof["status"] == "pr_merged":
            # The merge commit itself was never stored.
            await service.record_merge(
                case_id, merge_sha=None, resolution_path=ResolutionPath.EMBRY0, pr_url=pr.url, at=flipped_at
            )
        else:
            await service.close_pr(case_id, url=pr.url, at=flipped_at)
