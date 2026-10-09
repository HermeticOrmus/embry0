"""When may a ticket that embry0 already tracks be dispatched again (#284).

The Linear trigger used to dedupe on the ticket identifier forever: once a row
existed, no later label could start a job, so a ticket whose only job had
failed was stranded until someone reached for the API. This module is the one
definition of "dispatchable again" — pure, no I/O — shared by the webhook path
and the periodic reconciler so the two can never disagree.

The rule, evaluated over the embry0 issue row and *all* of its jobs:

* a **live** job (``pending`` / ``running`` / ``awaiting_input`` / ``paused``)
  always dedupes — work is in flight;
* a **pull request** is judged by its ``delivery_state`` (#280), not by the
  job's ``status``, which keeps ``failed`` whatever happens to the PR (#351):
  - merged (``delivery_state='merged'`` or a ``pr_merged`` job) dedupes: the
    work was delivered;
  - closed unmerged (``delivery_state='closed'`` or a ``pr_closed`` job) frees
    the ticket: a person rejected that PR;
  - open, on a ``failed`` / ``cancelled`` / ``expired`` job, does not dedupe:
    it holds the work that job pushed before it stopped, and the next run
    continues on that PR (#349, ``_run_workflow``);
  - open on any other job (a completed job's PR awaiting review), or with no
    delivery fact at all, dedupes;
* the embry0 issue must be ``open`` — ``closed`` / ``cancelled`` are never
  resurrected by a label, and any other status means a job owns the issue;
* with no job at all (a stranded dispatch) the ticket is dispatchable;
* otherwise only a latest job in ``failed`` / ``cancelled`` / ``expired``
  qualifies. ``completed`` / ``partial`` / ``pr_closed`` mean work was
  delivered or a human rejected it: a retry is a human decision, taken
  through the API, not something a label repeats.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from embry0.services.merge_sync.facts import DeliveryState
from embry0.storage.schemas import IssueStatus, JobStatus

LIVE_JOB_STATUSES: frozenset[str] = frozenset(
    {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.AWAITING_INPUT, JobStatus.PAUSED}
)
REDISPATCHABLE_JOB_STATUSES: frozenset[str] = frozenset({JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.EXPIRED})


@dataclass(frozen=True)
class DispatchEligibility:
    """Verdict for one tracked ticket.

    ``reason`` is a stable machine code (it keys the once-per-reason ticket
    comment); ``detail`` is the terse sentence for logs and API responses.
    ``retry_of`` is the failed job a fresh dispatch would follow — empty for a
    first dispatch. ``blocking_job`` is the job a refusal is about, so the
    tracker can word its own comment.
    """

    eligible: bool
    reason: str = ""
    detail: str = ""
    retry_of: dict[str, Any] | None = None
    blocking_job: dict[str, Any] | None = None


def dispatch_eligibility(issue_row: dict[str, Any], jobs: Sequence[dict[str, Any]]) -> DispatchEligibility:
    """Apply the rule above. ``jobs`` is every job of the issue, newest first."""
    for job in jobs:
        if job.get("status") in LIVE_JOB_STATUSES:
            return DispatchEligibility(
                False,
                "live_job",
                f"job `{job.get('job_id')}` is still {job.get('status')} for this ticket",
                blocking_job=dict(job),
            )
    for job in jobs:
        delivery = job.get("delivery_state")
        if job.get("status") == JobStatus.PR_MERGED or delivery == DeliveryState.MERGED:
            return DispatchEligibility(
                False,
                "work_delivered",
                f"job `{job.get('job_id')}` already delivered a merged pull request",
                blocking_job=dict(job),
            )
        if not job.get("pr_url") or job.get("status") == JobStatus.PR_CLOSED or delivery == DeliveryState.CLOSED:
            continue
        if delivery == DeliveryState.PR_OPEN and job.get("status") in REDISPATCHABLE_JOB_STATUSES:
            continue  # the next run continues on this PR (#349)
        return DispatchEligibility(
            False, "open_pr", f"an earlier job already opened {job['pr_url']}", blocking_job=dict(job)
        )

    status = issue_row.get("status")
    if status != IssueStatus.OPEN:
        return DispatchEligibility(False, f"issue_{status}", f"the embry0 issue is `{status}`, not `open`")

    if not jobs:
        return DispatchEligibility(True)
    latest = jobs[0]
    if latest.get("status") in REDISPATCHABLE_JOB_STATUSES:
        return DispatchEligibility(True, retry_of=dict(latest))
    return DispatchEligibility(
        False,
        "work_delivered",
        f"the latest job `{latest.get('job_id')}` ended `{latest.get('status')}`; "
        "start a retry from the embry0 dashboard if one is wanted",
        blocking_job=dict(latest),
    )
