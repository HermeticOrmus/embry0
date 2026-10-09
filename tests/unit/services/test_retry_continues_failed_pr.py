"""A retry continues on the PR a failed job left, instead of being skipped (#349).

A job that fails after pushing now gets a PR. The pre-graph duplicate check
(RAV-1079) would then find that open PR on the next run of the ticket and END
the new job as ``completed`` against the failed job's unfinished work, closing
the embry0 issue. Instead, an open PR whose newest owner is a failed,
cancelled or expired job of the same issue is adopted: the new job continues
on it as a follow-up does. Every other open PR is still a real duplicate.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embry0.services.issue_executor import IssueExecutor

ISSUE_ID = "iss-eafd1fec0000"
PR = {
    "number": 366,
    "title": "fix(msgraph): stop stub deals",
    "head_ref": "embry0/iss-eafd1fec-eng-1066-watcher",
    "url": "https://github.com/o/r/pull/366",
    "author": "embry0",
    "issue_refs": [],
    "closes_numbers": [],
    "issue_prefix": "iss-eafd1fec",
}


def _executor(owners: list[dict[str, Any]] | Exception) -> tuple[IssueExecutor, dict[str, Any]]:
    jobs = MagicMock()
    jobs.get = AsyncMock(return_value={"trace_id": None, "pipeline_config": None})
    jobs.update = AsyncMock()
    if isinstance(owners, Exception):
        jobs.find_by_pr_url = AsyncMock(side_effect=owners)
    else:
        jobs.find_by_pr_url = AsyncMock(return_value=owners)
    issues = MagicMock()
    issues.get = AsyncMock(return_value=None)
    issues.update = AsyncMock()
    issues.update_parent_status = AsyncMock()
    captured: dict[str, Any] = {}

    async def fake_stream(input_value, issue_id, job_id, state_sink=None):
        captured["initial_state"] = input_value
        return None, False, None

    ex = IssueExecutor.__new__(IssueExecutor)
    ex._jobs = jobs
    ex._issues = issues
    ex._traces = MagicMock()
    ex._registry = MagicMock()
    ex._database_url = ""
    ex._audit_log_path = None
    ex._db = None
    ex._inputs = None
    ex._config = SimpleNamespace(environment_secret_key="k")
    ex._sandbox = None
    ex._agent_runner = None
    ex._proxy = None
    ex._event_bus = None
    ex._env_repo = None
    ex._github_token = "tok"
    ex._background_tasks = set()
    ex._tasks_by_job = {}
    ex._execute_workflow_stream = fake_stream  # type: ignore[assignment]
    ex._cleanup_sandbox = AsyncMock()  # type: ignore[assignment]
    return ex, captured


def _owner(status: str, issue_id: str = ISSUE_ID, job_id: str = "job-earlier") -> dict[str, Any]:
    return {
        "job_id": job_id,
        "issue_id": issue_id,
        "status": status,
        "pr_url": PR["url"],
        "error_message": "QA failures unresolved after 2 rounds",
    }


async def _run(owners: list[dict[str, Any]] | Exception) -> tuple[IssueExecutor, dict[str, Any]]:
    ex, captured = _executor(owners)
    issue = {"title": "Stop stub deals", "body": "", "repo": "o/r", "tracker": "linear", "tracker_key": "ENG-9999"}
    with patch("embry0.services.github_prs.OpenPRService.list_open_prs", new=AsyncMock(return_value=[dict(PR)])):
        await ex._run_workflow(ISSUE_ID, "job-new", issue)
    return ex, captured


def _skipped(ex: IssueExecutor) -> bool:
    """The duplicate skip: completed, linked to the PR, with its "Skipped:" summary.

    (The harness's stream returns no final state, which the normal path also
    marks completed, so the status alone does not tell a skip apart.)
    """
    return any(
        str(c.kwargs.get("result_summary") or "").startswith("Skipped:") for c in ex._jobs.update.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "expired"])
async def test_a_retry_continues_on_the_pr_its_failed_predecessor_left(status: str) -> None:
    ex, captured = await _run([_owner(status)])

    assert not _skipped(ex), "a retry must not be ended as completed against a failed job's unfinished PR"
    state = captured.get("initial_state")
    assert state is not None, "the graph must run"
    assert state["pr_url"] == PR["url"], state.get("pr_url")
    assert state["context"]["ref"] == PR["head_ref"], f"the retry must work on the PR's branch: {state['context']}"
    ex._jobs.update.assert_any_await("job-new", pr_url=PR["url"])


@pytest.mark.asyncio
async def test_the_retry_is_told_why_the_earlier_run_stopped() -> None:
    _, captured = await _run([_owner("failed")])

    note = captured["initial_state"]["additional_context"]
    assert "job-earlier" in note and "QA failures unresolved after 2 rounds" in note, note
    assert "do not open another pull request" in note.lower()


@pytest.mark.asyncio
async def test_a_completed_jobs_open_pr_is_still_a_duplicate() -> None:
    ex, captured = await _run([_owner("completed")])

    assert _skipped(ex) and "initial_state" not in captured, "work awaiting review is not a retry's to take over"


@pytest.mark.asyncio
async def test_another_issues_failed_pr_is_still_a_duplicate() -> None:
    ex, captured = await _run([_owner("failed", issue_id="iss-0000aaaa0000")])

    assert _skipped(ex) and "initial_state" not in captured


@pytest.mark.asyncio
async def test_a_pr_no_job_owns_is_still_a_duplicate() -> None:
    ex, captured = await _run([])

    assert _skipped(ex) and "initial_state" not in captured


@pytest.mark.asyncio
async def test_a_failed_owner_lookup_keeps_the_duplicate_skip() -> None:
    ex, captured = await _run(RuntimeError("db down"))

    assert _skipped(ex) and "initial_state" not in captured


@pytest.mark.asyncio
async def test_the_newest_owner_decides() -> None:
    """A later job that completed on that PR made it a delivery awaiting review."""
    ex, captured = await _run([_owner("completed", job_id="job-later"), _owner("failed")])

    assert _skipped(ex) and "initial_state" not in captured
