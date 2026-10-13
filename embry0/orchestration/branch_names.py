"""The working-branch name a job pushes to — one derivation, two readers.

The developer node names the branch; merge sync (#279) re-derives the same
name from the stored job row to recognise a pull request opened from that
branch by someone else (a human finishing a failed job's work). Pure, so the
two can never disagree.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_UNSAFE = re.compile(r"[^a-z0-9-]+")


def shared_branch_name(*, issue_id: str | None, task: str | None) -> str:
    """``embry0/{issue id}-{task slug}``: the name every job of one issue derives.

    Shared by design: every job dispatched on the issue derives it, so it is
    never certainly one job's own (#478). Before #558 a job with no issue
    derived it as well, with ``unknown`` for the id, so jobs whose tasks begin
    the same way shared it too. Those jobs' rows still re-derive it, which is
    why merge sync keeps matching it.

    git rejects refs containing .., ~, ^, :, ?, *, [, \\, control chars, or
    names ending in .lock; the substitution strips every non-safe char in one
    pass.
    """
    short_id = issue_id[:12] if issue_id else "unknown"
    slug = _UNSAFE.sub("-", (task or "")[:30].lower()).strip("-") or "task"
    return f"embry0/{short_id}-{slug}"


def job_branch_name(
    *, issue_id: str | None, task: str | None, job_id: str | None, context: Mapping[str, Any] | None = None
) -> str:
    """Git-ref-safe branch for a job.

    RAV-1092: a job dispatched against an EXISTING branch (PR-comment
    follow-ups) carries it as ``context["ref"]`` — already checked out by
    workspace init and validated there. It wins: the derived name is for jobs
    that create their own branch.

    A job of an issue derives the issue's :func:`shared_branch_name`. A job
    with no issue derives its own (#558): the task slug alone is shared by
    every job whose task begins the same way, and two such jobs dispatched
    together both found the name free and pushed over each other. The job id
    is required so that no caller can derive an issue-less name without it;
    a caller with no job id gets the shared name, as before #558.
    """
    ref = (context or {}).get("ref")
    if ref:
        return str(ref)
    shared = shared_branch_name(issue_id=issue_id, task=task)
    if issue_id or not job_id:
        return shared
    return taken_branch_name(shared, job_id)


def taken_branch_name(derived: str, job_id: str | None) -> str:
    """The job's own branch: ``derived`` plus the first 8 characters of its id.

    A job of an issue uses it when origin already holds the issue's shared
    name (#478); a job with no issue always uses it (#558). The job id makes
    the name the job's own, and keeps it derivable from the stored row for
    merge sync. The ``iss-…`` prefix is untouched, so the open-PR duplicate
    check (``parse_embry0_branch_issue_prefix``) still recognises it.
    """
    suffix = _UNSAFE.sub("-", (job_id or "").removeprefix("job-").lower()).strip("-")[:8] or "retry"
    return f"{derived}-{suffix}"


def job_branch_names(
    *, issue_id: str | None, task: str | None, context: Mapping[str, Any] | None = None, job_id: str | None = None
) -> frozenset[str]:
    """Every branch a job may have pushed to: the shared name, or its own.

    A job row does not record which of the two its developer used, so a
    reader matching branches to jobs (merge sync) accepts both. For a job of
    an issue either is possible (#478). A job with no issue uses its own since
    #558, and the shared name stays in the set for the rows created before
    then. A follow-up job works on its PR's existing branch and has only that
    one.
    """
    ref = (context or {}).get("ref")
    if ref:
        return frozenset({str(ref)})
    shared = shared_branch_name(issue_id=issue_id, task=task)
    return frozenset({shared, taken_branch_name(shared, job_id)})


def job_push_branch(
    *,
    issue_id: str | None,
    task: str | None,
    job_id: str | None,
    context: Mapping[str, Any] | None = None,
    chosen: str | None = None,
) -> str:
    """The one branch a job's sandbox may push, as its git-proxy enrollment names it (#559).

    ``chosen`` is the branch the developer settled on (``state["branch_name"]``),
    which every later run and every resume reads back, so it wins. Before the
    developer chooses, a follow-up job has its PR's branch, and every other
    job its own name: :func:`taken_branch_name`, which no other job derives.
    The issue's shared name is granted only once the developer has found it
    free on origin (``ProxyManager.set_git_push_branches``), so a branch an
    earlier job pushed (#478) is never writable from this sandbox.
    """
    if chosen:
        return str(chosen)
    ref = (context or {}).get("ref")
    if ref:
        return str(ref)
    return taken_branch_name(shared_branch_name(issue_id=issue_id, task=task), job_id)
