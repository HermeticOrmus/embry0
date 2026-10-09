"""What a forge says about one pull request, and how that lands on a job.

Pure — no I/O. Two things live here:

* :class:`PullRequestFact` — the forge's view of a PR at one moment, built the
  same way from a webhook payload, a ``GET /pulls/{n}`` body or one entry of a
  ``GET /pulls`` listing (the three carry the same fields).
* :func:`resolve_delivery` — the ordering rule that decides whether a fact may
  replace what a job already records. Webhooks are redelivered and arrive out
  of order, and a sweep's snapshot can be overtaken by a webhook while it is
  in flight, so every fact is dated by the FORGE (never by arrival) and an
  older fact never replaces a newer one.

``delivery_state`` versus ``status`` (#280): ``status`` says how the JOB ended
(``completed`` / ``failed`` / …); ``delivery_state`` says what became of the
PULL REQUEST it produced. They are independent: a job can end ``failed`` and
its PR still merge. ``pr_merged`` / ``pr_closed`` keep their meaning — a job
that ``completed`` and whose PR was then merged / closed unmerged — so
``status = pr_merged`` implies ``delivery_state = merged``, never the reverse.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from embry0.storage.repo_renames import normalize_repo_slug, normalize_repo_url


class DeliveryState(StrEnum):
    NONE = "none"  # the job has no pull request
    PR_OPEN = "pr_open"
    MERGED = "merged"
    CLOSED = "closed"  # closed without merging


@dataclass(frozen=True)
class PullRequestFact:
    """One PR as the forge reports it. ``url`` and ``repo`` are normalised."""

    repo: str
    number: int | None
    url: str
    state: DeliveryState
    at: datetime | None  # forge time of `state`: merged_at / closed_at / updated_at
    head_ref: str = ""
    head_sha: str | None = None
    merge_sha: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # A fork's branch name is the fork owner's to choose: never matched to a job.
    from_fork: bool = False


def parse_forge_time(value: Any) -> datetime | None:
    """A GitHub ISO-8601 timestamp (``…Z``) as an aware datetime, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def fact_from_github(pr: Mapping[str, Any], *, repo: str = "") -> PullRequestFact | None:
    """Build a fact from a GitHub pull-request object; None when it has no URL."""
    url = str(pr.get("html_url") or "")
    if not url:
        return None
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name") or ""
    head = pr.get("head") or {}
    merged_at = parse_forge_time(pr.get("merged_at"))
    closed_at = parse_forge_time(pr.get("closed_at"))
    updated_at = parse_forge_time(pr.get("updated_at"))
    if merged_at is not None or pr.get("merged") is True:
        state, at = DeliveryState.MERGED, merged_at or closed_at
    elif str(pr.get("state") or "") == "closed":
        state, at = DeliveryState.CLOSED, closed_at or updated_at
    else:
        state, at = DeliveryState.PR_OPEN, updated_at
    number = pr.get("number")
    head_repo = (head.get("repo") or {}).get("full_name") if "repo" in head else base_repo
    from_fork = bool(base_repo) and str(head_repo or "").lower() != str(base_repo).lower()
    return PullRequestFact(
        repo=normalize_repo_slug(str(repo or base_repo)),
        number=number if isinstance(number, int) else None,
        url=normalize_repo_url(url),
        state=state,
        at=at,
        head_ref=str(head.get("ref") or ""),
        head_sha=str(head.get("sha")) if head.get("sha") else None,
        merge_sha=str(pr.get("merge_commit_sha"))
        if state is DeliveryState.MERGED and pr.get("merge_commit_sha")
        else None,
        created_at=parse_forge_time(pr.get("created_at")),
        updated_at=updated_at,
        from_fork=from_fork,
    )


@dataclass(frozen=True)
class DeliveryChange:
    """What to write on a job. ``state_changed`` is False for a date-only write."""

    state: DeliveryState
    at: datetime | None
    state_changed: bool


def resolve_delivery(
    current_state: str,
    current_at: datetime | None,
    fact_state: DeliveryState,
    fact_at: datetime | None,
    *,
    advance_same_state: bool = True,
) -> DeliveryChange | None:
    """The write a fact causes on a job, or None when it changes nothing.

    * ``merged`` is final — a forge cannot un-merge — so once recorded only its
      missing date is ever filled in, and a merge replaces anything.
    * Otherwise the newer fact wins by FORGE time; an undated stored state
      (rows migrated from ``status``) yields to any fact, and an undated fact
      yields to any dated state.
    * The same state with a later date only moves the date, so that a late
      redelivery of an older ``closed`` cannot undo a ``reopened``. A sweep
      passes ``advance_same_state=False``: its next pass corrects anything a
      stale date let through, and it must not rewrite a job row every time
      someone comments on an open PR.
    """
    if current_state == DeliveryState.MERGED:
        if fact_state is DeliveryState.MERGED and current_at is None and fact_at is not None:
            return DeliveryChange(DeliveryState.MERGED, fact_at, state_changed=False)
        return None
    if fact_state is DeliveryState.MERGED:
        return DeliveryChange(DeliveryState.MERGED, fact_at, state_changed=True)
    if current_state == fact_state:
        if fact_at is None:
            return None
        if current_at is None or (advance_same_state and fact_at > current_at):
            return DeliveryChange(fact_state, fact_at, state_changed=False)
        return None
    if current_at is not None and (fact_at is None or fact_at < current_at):
        return None
    return DeliveryChange(fact_state, fact_at, state_changed=True)
