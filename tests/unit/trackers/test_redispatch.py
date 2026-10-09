"""dispatch_eligibility — which tracked tickets a label may dispatch again (#284)."""

from __future__ import annotations

from typing import Any

import pytest

from embry0.trackers.redispatch import dispatch_eligibility


def _job(status: str, job_id: str = "job-1", pr_url: str | None = None, delivery: str | None = None) -> dict[str, Any]:
    return {"job_id": job_id, "status": status, "pr_url": pr_url, "delivery_state": delivery}


_OPEN = {"id": "iss-1", "status": "open"}


def test_a_row_with_no_job_is_dispatchable() -> None:
    verdict = dispatch_eligibility(_OPEN, [])
    assert verdict.eligible and verdict.retry_of is None


@pytest.mark.parametrize("status", ["failed", "cancelled", "expired"])
def test_terminal_failed_latest_job_is_dispatchable(status: str) -> None:
    verdict = dispatch_eligibility(_OPEN, [_job(status)])
    assert verdict.eligible
    assert verdict.retry_of is not None and verdict.retry_of["job_id"] == "job-1"


@pytest.mark.parametrize("status", ["pending", "running", "awaiting_input", "paused"])
def test_a_live_job_dedupes(status: str) -> None:
    verdict = dispatch_eligibility({"id": "iss-1", "status": "triaging"}, [_job(status)])
    assert not verdict.eligible and verdict.reason == "live_job"
    assert "job-1" in verdict.detail


def test_a_live_job_dedupes_even_behind_a_newer_failed_one() -> None:
    verdict = dispatch_eligibility(_OPEN, [_job("failed", "job-2"), _job("running", "job-1")])
    assert not verdict.eligible and verdict.reason == "live_job"


def test_an_open_pr_dedupes_even_when_the_latest_job_failed() -> None:
    jobs = [_job("failed", "job-2"), _job("completed", "job-1", pr_url="https://github.com/o/r/pull/9")]
    verdict = dispatch_eligibility(_OPEN, jobs)
    assert not verdict.eligible and verdict.reason == "open_pr"
    assert "pull/9" in verdict.detail


@pytest.mark.parametrize("status", ["completed", "partial", "pr_merged", "pr_closed"])
def test_delivered_work_is_not_redispatched(status: str) -> None:
    verdict = dispatch_eligibility(_OPEN, [_job(status)])
    assert not verdict.eligible and verdict.reason == "work_delivered"


@pytest.mark.parametrize("status", ["closed", "cancelled", "triaging", "in_progress"])
def test_only_an_open_issue_is_dispatchable(status: str) -> None:
    verdict = dispatch_eligibility({"id": "iss-1", "status": status}, [_job("failed")])
    assert not verdict.eligible and verdict.reason == f"issue_{status}"


# ---- the exhaustive table: job status x pr_url x delivery x issue status ---

_JOB_STATUSES = [
    "pending",
    "running",
    "awaiting_input",
    "paused",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "expired",
    "pr_merged",
    "pr_closed",
]
_ISSUE_STATUSES = ["open", "triaging", "in_progress", "awaiting_input", "paused", "closed", "cancelled"]
_PR = "https://github.com/o/r/pull/9"


_DELIVERIES = [None, "none", "pr_open", "merged", "closed"]


def _expected(job_status: str, pr_url: str | None, issue_status: str, delivery: str | None = None) -> tuple[bool, str]:
    if job_status in {"pending", "running", "awaiting_input", "paused"}:
        return False, "live_job"
    if job_status == "pr_merged" or delivery == "merged":
        return False, "work_delivered"
    pr_gone = job_status == "pr_closed" or delivery == "closed"
    continued = delivery == "pr_open" and job_status in {"failed", "cancelled", "expired"}
    if pr_url and not pr_gone and not continued:
        return False, "open_pr"
    if issue_status != "open":
        return False, f"issue_{issue_status}"
    if job_status in {"failed", "cancelled", "expired"}:
        return True, ""
    return False, "work_delivered"


@pytest.mark.parametrize("delivery", _DELIVERIES)
@pytest.mark.parametrize("issue_status", _ISSUE_STATUSES)
@pytest.mark.parametrize("pr_url", [None, _PR])
@pytest.mark.parametrize("job_status", _JOB_STATUSES)
def test_single_job_table(job_status: str, pr_url: str | None, issue_status: str, delivery: str | None) -> None:
    verdict = dispatch_eligibility(
        {"id": "iss-1", "status": issue_status}, [_job(job_status, pr_url=pr_url, delivery=delivery)]
    )
    assert (verdict.eligible, verdict.reason) == _expected(job_status, pr_url, issue_status, delivery)


@pytest.mark.parametrize("delivery", _DELIVERIES)
@pytest.mark.parametrize("earlier_pr_url", [None, _PR])
@pytest.mark.parametrize("earlier", _JOB_STATUSES)
def test_an_earlier_job_blocks_a_retry_of_a_newer_failed_one(
    earlier: str, earlier_pr_url: str | None, delivery: str | None
) -> None:
    """The latest job failing says nothing about what an older job left behind."""
    jobs = [_job("failed", "job-2"), _job(earlier, "job-1", pr_url=earlier_pr_url, delivery=delivery)]
    verdict = dispatch_eligibility(_OPEN, jobs)
    blocked, reason = _expected(earlier, earlier_pr_url, "open", delivery)
    if reason in {"live_job", "open_pr"} or earlier == "pr_merged" or delivery == "merged":
        assert (verdict.eligible, verdict.reason) == (False, reason)
    else:
        assert verdict.eligible  # nothing older stands in the way of the failed latest


def test_a_failed_job_with_no_delivery_fact_still_dedupes() -> None:
    """#279: without merge sync's word, `failed` + pr_url may be a merged change."""
    verdict = dispatch_eligibility(_OPEN, [_job("failed", pr_url=_PR)])
    assert not verdict.eligible and verdict.reason == "open_pr"


def test_a_failed_job_whose_pr_was_closed_unmerged_frees_its_ticket() -> None:
    """#351: status stays `failed` whatever happens to the PR; delivery_state says it was closed."""
    verdict = dispatch_eligibility(_OPEN, [_job("failed", pr_url=_PR, delivery="closed")])
    assert verdict.eligible, f"a PR a person closed unmerged must not block the ticket forever: {verdict}"
    assert verdict.retry_of is not None and verdict.retry_of["job_id"] == "job-1"


def test_a_failed_job_whose_pr_merged_counts_as_delivered() -> None:
    verdict = dispatch_eligibility(_OPEN, [_job("failed", pr_url=_PR, delivery="merged")])
    assert not verdict.eligible and verdict.reason == "work_delivered", verdict


def test_a_failed_jobs_open_pr_is_continued_not_a_block() -> None:
    """#349: the next run continues on that PR, so the ticket may be dispatched."""
    verdict = dispatch_eligibility(_OPEN, [_job("failed", pr_url=_PR, delivery="pr_open")])
    assert verdict.eligible, verdict
    assert verdict.retry_of is not None and verdict.retry_of["job_id"] == "job-1"


def test_a_completed_jobs_open_pr_still_dedupes() -> None:
    verdict = dispatch_eligibility(_OPEN, [_job("completed", pr_url=_PR, delivery="pr_open")])
    assert not verdict.eligible and verdict.reason == "open_pr"


def test_a_completed_job_whose_pr_was_closed_still_needs_a_person() -> None:
    """Unchanged: a person rejected delivered work; a retry is theirs to start."""
    verdict = dispatch_eligibility(_OPEN, [_job("pr_closed", pr_url=_PR, delivery="closed")])
    assert not verdict.eligible and verdict.reason == "work_delivered"


def test_a_failure_after_a_merged_pr_is_not_retried() -> None:
    verdict = dispatch_eligibility(_OPEN, [_job("failed", "job-2"), _job("pr_merged", "job-1")])
    assert not verdict.eligible and verdict.reason == "work_delivered"
