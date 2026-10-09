"""DeliveryRecorder — the one code path that records what a forge says about a PR.

The PR webhook, the periodic sweep and the one-off backfill all hand a
:class:`PullRequestFact` to :meth:`DeliveryRecorder.record`, which

1. finds the jobs the PR belongs to — by stored PR URL (renamed-repo forms
   included), and, for jobs that never stored one, by the branch the job
   pushes to (re-derived from the job row, so ``embry0/unknown-*`` branches
   and PRs a human opened from a failed job's branch are found);
2. writes ``delivery_state`` on every such job WHATEVER its status, under the
   ordering rule in :mod:`.facts`, and lets ``status`` follow only where its
   existing meaning allows (``completed`` → ``pr_merged`` / ``pr_closed``);
3. closes the embry0 issue when a merge is first recorded;
4. reports the same fact to the healing-case ledger.

It only records. It never dispatches, comments or calls a forge, so a
redelivered, reordered or replayed fact is always safe to hand in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

import structlog

from embry0.orchestration.branch_names import job_branch_names
from embry0.services.merge_sync.facts import DeliveryState, PullRequestFact, resolve_delivery
from embry0.storage.repo_renames import repo_slug_variants, repo_url_variants
from embry0.storage.repositories.job_delivery import DeliveryWrite, JobDeliveryRepository
from embry0.storage.schemas import JobStatus

logger = structlog.get_logger(__name__)

# A PR can only come from a job created before it; forge and DB clocks differ.
_CLOCK_SKEW = timedelta(minutes=5)
# How long before a PR was opened a job may have been created and still be the
# one whose branch it is. Bounds the candidate query, nothing else.
BRANCH_MATCH_WINDOW = timedelta(days=90)
# Most jobs one PR may be credited to by branch: its follow-ups, plus the one
# job that pushed it. A backstop on the blast radius of a single fact — see
# ``DeliveryRecorder.matching_jobs`` for why a branch is not a unique key.
MAX_BRANCH_MATCHES = 5


class _Issues(Protocol):
    async def update(self, issue_id: str | None, **fields: Any) -> None: ...


def aligned_status(status: str, delivery_state: str) -> str | None:
    """The status a job should move to now that its delivery is known, or None.

    ``pr_merged`` / ``pr_closed`` keep their meaning — a job that COMPLETED and
    whose PR was then merged / closed unmerged — so only ``completed`` (and
    ``pr_closed``, when the PR was reopened) ever moves. ``failed``,
    ``cancelled``, ``partial`` … stay what they are: how the job ended is a
    different fact from what became of its PR.
    """
    if status == JobStatus.COMPLETED:
        return _COMPLETED_FOLLOWS.get(delivery_state)
    if status == JobStatus.PR_CLOSED:
        return _PR_CLOSED_FOLLOWS.get(delivery_state)
    return None


_COMPLETED_FOLLOWS: dict[str, str] = {
    DeliveryState.MERGED.value: JobStatus.PR_MERGED.value,
    DeliveryState.CLOSED.value: JobStatus.PR_CLOSED.value,
}
# A closed PR that was reopened, and perhaps then merged.
_PR_CLOSED_FOLLOWS: dict[str, str] = {
    DeliveryState.MERGED.value: JobStatus.PR_MERGED.value,
    DeliveryState.PR_OPEN.value: JobStatus.COMPLETED.value,
}


@dataclass(frozen=True)
class JobChange:
    """One job row merge sync changed (or, on a dry run, would change)."""

    job_id: str
    matched_by: str  # "pr_url" | "branch"
    delivery_from: str
    delivery_to: str
    status_from: str
    status_to: str
    pr_url_linked: bool

    @property
    def material(self) -> bool:
        """False for a write that only filled in a missing forge date."""
        return self.delivery_from != self.delivery_to or self.status_from != self.status_to or self.pr_url_linked

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RecordResult:
    pr_url: str
    state: str
    matched_jobs: list[str] = field(default_factory=list)
    changes: list[JobChange] = field(default_factory=list)
    issues_closed: list[str] = field(default_factory=list)
    case_id: str | None = None
    case_outcome: str | None = None

    @property
    def matched(self) -> bool:
        return bool(self.matched_jobs) or self.case_id is not None


class DeliveryRecorder:
    def __init__(self, delivery_repo: JobDeliveryRepository, issues_repo: _Issues, healing: Any | None = None) -> None:
        self._delivery = delivery_repo
        self._issues = issues_repo
        self._healing = healing

    # ------------------------------------------------------------- matching

    async def matching_jobs(self, fact: PullRequestFact) -> list[tuple[dict[str, Any], str]]:
        """``(job row, matched_by)`` for every job this PR belongs to.

        By stored URL first — exact, and authoritative. Then, among the repo's
        jobs that have no PR URL:

        * every job dispatched ONTO this branch (``context.ref`` — a PR
          follow-up) belongs to the PR, whenever it was created: that branch
          was handed to it, not derived from its task;
        * a job whose branch this is only by DERIVATION is credited when no
          job claims the PR by URL — and then exactly ONE of them, the newest
          created before the PR was opened.

        That last rule is the point of this method. A derived branch is
        ``embry0/{issue id or "unknown"}-{first 30 chars of the task}``, so
        every retry of an issue derives the SAME branch, and so did every job
        with no issue whose task merely began the same way until #558 gave
        those jobs their own: the live rows hold 13 colliding ``(repo,
        branch)`` names, one shared by 37 jobs. Only one job can have
        pushed the branch a PR was opened from, so crediting every candidate
        would write that PR's merge onto 36 jobs that never pushed anything.
        A job row carries neither its branch nor a pushed sha, so "which one"
        cannot be answered exactly; the newest job that existed before the PR
        did is the best available answer, and an ambiguous choice is logged
        rather than made silently.
        """
        matched = [(job, "pr_url") for job in await self._delivery.find_by_pr_urls(repo_url_variants(fact.url))]
        if not (fact.head_ref and fact.repo and fact.created_at is not None) or fact.from_fork:
            return matched
        opened_by = fact.created_at + _CLOCK_SKEW
        candidates = await self._delivery.find_unlinked_in_repo(
            repo_slug_variants(fact.repo), created_before=opened_by, window=BRANCH_MATCH_WINDOW
        )
        claimed = bool(matched)
        follow_ups: list[dict[str, Any]] = []
        derived: list[dict[str, Any]] = []  # oldest first, as the query orders them
        for job in candidates:
            if fact.head_ref not in branches_of(job):
                continue
            if _follow_up_ref(job):
                follow_ups.append(job)
            elif not claimed and job["created_at"] <= opened_by:
                derived.append(job)
        if len(derived) > 1:
            logger.info(
                "merge_sync_branch_match_ambiguous",
                pr_url=fact.url,
                branch=fact.head_ref,
                candidates=len(derived),
                credited=derived[-1]["job_id"],
            )
        by_branch = follow_ups + derived[-1:]
        if len(by_branch) > MAX_BRANCH_MATCHES:
            # Only reachable if one branch was handed to more jobs than a PR
            # can plausibly have follow-ups. Cap rather than fan out, and say
            # so — no single fact may rewrite an unbounded number of rows.
            logger.warning(
                "merge_sync_branch_match_capped",
                pr_url=fact.url,
                branch=fact.head_ref,
                matched=len(by_branch),
                cap=MAX_BRANCH_MATCHES,
            )
            by_branch = by_branch[:MAX_BRANCH_MATCHES]
        return matched + [(job, "branch") for job in by_branch]

    # -------------------------------------------------------------- record

    async def record(
        self,
        fact: PullRequestFact,
        *,
        source: str,
        event_key: str | None = None,
        dry_run: bool = False,
    ) -> RecordResult:
        """Record ``fact``. ``source`` is ``webhook`` / ``sweep`` / ``backfill``.

        A sweep's snapshot does not move the date of a state it merely
        confirms (see ``resolve_delivery``); everything else is identical
        across sources. ``dry_run`` computes the same result and writes
        nothing — it is what shadow mode and the DB-equals-GitHub check run.
        """
        result = RecordResult(pr_url=fact.url, state=fact.state.value)
        jobs = await self.matching_jobs(fact)
        result.matched_jobs = [job["job_id"] for job, _ in jobs]
        advance = source == "webhook"

        urls = {u.lower() for u in repo_url_variants(fact.url)}

        def decide(row: dict[str, Any]) -> DeliveryWrite | None:
            if row["pr_url"] and row["pr_url"].lower() not in urls:
                return None  # linked to another PR since it was matched by branch
            change = resolve_delivery(
                row["delivery_state"], row["delivery_state_at"], fact.state, fact.at, advance_same_state=advance
            )
            delivery = change.state.value if change else row["delivery_state"]
            status = aligned_status(row["status"], delivery)
            link = fact.url if not row["pr_url"] else None
            if change is None and status is None and link is None:
                return None
            return DeliveryWrite(
                delivery_state=change.state.value if change else None,
                delivery_state_at=change.at if change else None,
                pr_url=link,
                status=status,
            )

        newly_merged_issues: list[str] = []
        states_after: set[str] = set()
        for job, matched_by in jobs:
            if dry_run:
                write = decide(job)
                before = job
            else:
                applied = await self._delivery.apply(job["job_id"], decide)
                before, write = applied if applied else (job, None)
            states_after.add((write.delivery_state if write else None) or before["delivery_state"])
            if write is None:
                continue
            change = JobChange(
                job_id=job["job_id"],
                matched_by=matched_by,
                delivery_from=before["delivery_state"],
                delivery_to=write.delivery_state or before["delivery_state"],
                status_from=before["status"],
                status_to=write.status or before["status"],
                pr_url_linked=write.pr_url is not None,
            )
            result.changes.append(change)
            if (
                change.delivery_to == DeliveryState.MERGED
                and change.delivery_from != DeliveryState.MERGED
                and job.get("issue_id")
                and job["issue_id"] not in newly_merged_issues
            ):
                newly_merged_issues.append(job["issue_id"])

        if not dry_run:
            for issue_id in newly_merged_issues:
                if await self._close_issue(issue_id):
                    result.issues_closed.append(issue_id)
            # The jobs' ordering rule already judged this fact: when none of
            # them ends up in the fact's state it was a stale redelivery, and
            # the ledger must not hear it either.
            if not jobs or fact.state.value in states_after:
                await self._report_to_ledger(fact, [job for job, _ in jobs], result, event_key=event_key, source=source)

        if result.changes:
            logger.info(
                "merge_sync_would_record" if dry_run else "merge_sync_recorded",
                source=source,
                pr_url=fact.url,
                state=fact.state.value,
                jobs=[c.as_dict() for c in result.changes],
                issues_closed=result.issues_closed,
            )
        return result

    async def _close_issue(self, issue_id: str) -> bool:
        """Close the embry0 issue of a merged PR. A status the issue cannot
        leave (``cancelled``) or a concurrent writer is logged, never raised:
        the delivery fact is already recorded and must not be lost to it."""
        try:
            await self._issues.update(issue_id, status="closed")
            return True
        except Exception as exc:
            logger.warning("merge_sync_issue_close_skipped", issue_id=issue_id, error=str(exc))
            return False

    # -------------------------------------------------------------- ledger

    async def _report_to_ledger(
        self,
        fact: PullRequestFact,
        jobs: list[dict[str, Any]],
        result: RecordResult,
        *,
        event_key: str | None,
        source: str,
    ) -> None:
        """Hand the fact to the healing-case ledger, if this PR has a case.

        The case is the one that already owns the PR, else the case of a
        matched job's issue. ``resolution_path`` is ``embry0`` when one of the
        matched jobs belongs to that case and ``out_of_band`` otherwise (a PR
        attached to the case by ticket reference, #282). A PR with no job at
        all has no ordering rule behind it, so the ledger's own PR row is the
        guard against a stale ``opened`` or ``closed``; should a late
        redelivery still slip through, the next sweep records the forge's
        current answer. The ledger is an optional subsystem here: whatever it
        raises is logged and swallowed.
        """
        if self._healing is None:
            return
        try:
            case = await self._find_case(fact, jobs)
            if case is None:
                return
            result.case_id = case["id"]
            detail = await self._healing.get(case["id"]) or {}
            case_job_ids = {j["job_id"] for j in detail.get("jobs") or []}
            owners = [
                j
                for j in jobs
                if j["job_id"] in case_job_ids or (j.get("issue_id") and j["issue_id"] == case.get("issue_id"))
            ]
            known = next((p for p in detail.get("prs") or [] if p["url"] in repo_url_variants(fact.url)), None)
            url = known["url"] if known else fact.url
            # Sweeps have no delivery id, so the fact itself is the key and
            # the same observation is dropped however often it is made. An
            # UNDATED fact cannot key anything: every undated observation of
            # this PR in this state would share one key, and the first would
            # make all the others duplicates — including ones carrying new
            # information. Those go unkeyed and are deduped on content.
            key = event_key
            if key is None and fact.at is not None:
                key = f"merge-sync:{fact.url}:{fact.state.value}:{fact.at.isoformat()}"

            if fact.state is DeliveryState.MERGED:
                outcome = await self._healing.record_merge(
                    case["id"],
                    merge_sha=fact.merge_sha,
                    resolution_path="embry0" if owners else "out_of_band",
                    pr_url=url,
                    at=fact.at,
                    event_key=key,
                )
            elif fact.state is DeliveryState.CLOSED:
                if not jobs and known is not None and not _not_before(fact.at, known.get("opened_at")):
                    return  # a close older than the PR row's opening: a stale redelivery
                outcome = await self._healing.close_pr(case["id"], url=url, at=fact.at, event_key=key)
            else:
                # With jobs, their ordering rule already accepted this `open`;
                # without, it must be later than the recorded close.
                reopened = bool(
                    known is not None
                    and known["state"] == "closed"
                    and (jobs or (fact.at is not None and _not_before(fact.at, known.get("closed_at"))))
                )
                if known is not None and known["state"] == "merged":
                    return  # a merge is final
                if not jobs and known is not None and known["state"] == "closed" and not reopened:
                    return  # an `opened` older than the recorded close
                outcome = await self._healing.attach_pr(
                    case["id"],
                    url=url,
                    number=fact.number,
                    head_sha=fact.head_sha if fact.at is not None else None,
                    job_id=owners[0]["job_id"] if owners else None,
                    reopened=reopened,
                    at=fact.at,
                    event_key=key,
                )
            result.case_outcome = str(getattr(outcome, "outcome", "")) or None
            if result.case_outcome == "duplicate_conflict":
                logger.warning("merge_sync_ledger_duplicate_conflict", case_id=case["id"], pr_url=fact.url, key=key)
        except Exception:
            logger.warning("merge_sync_ledger_failed", pr_url=fact.url, source=source, exc_info=True)

    async def _find_case(self, fact: PullRequestFact, jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
        assert self._healing is not None
        for url in repo_url_variants(fact.url):
            case = await self._healing.find_by_pr_url(url)
            if case is not None:
                return dict(case)
        for issue_id in dict.fromkeys(j["issue_id"] for j in jobs if j.get("issue_id")):
            case = await self._healing.find_by_issue(issue_id)
            if case is not None:
                return dict(case)
        return None


def branches_of(job: dict[str, Any]) -> frozenset[str]:
    """The branches a stored job may have pushed to — the developer node's derivation.

    The shared name, or the job's own: a job of an issue uses its own when an
    earlier job's branch held the shared one (#478), and a job with no issue
    always has since #558. The row does not say which, so both match; the
    job's own name is unique to it, so a PR opened from it is credited to
    that job alone.
    """
    context = job.get("context")
    return job_branch_names(
        issue_id=job.get("issue_id"),
        task=job.get("task"),
        context=context if isinstance(context, dict) else None,
        job_id=job.get("job_id"),
    )


def _follow_up_ref(job: dict[str, Any]) -> bool:
    context = job.get("context")
    return isinstance(context, dict) and bool(context.get("ref"))


def _not_before(at: Any, reference: Any) -> bool:
    return at is None or reference is None or at >= reference
