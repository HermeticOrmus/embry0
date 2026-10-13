"""A job working a watcher-origin case is pinned to the full pipeline (#306).

The executor reads the case's origin from the ledger and puts
``pin_full_pipeline`` on the graph's initial state; plan_route_node does the
rest (tests/unit/workflows/test_plan_route_node.py).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.services.issue_executor import IssueExecutor


def _executor(recorder) -> tuple[IssueExecutor, dict]:
    captured: dict = {}

    async def _stream(input_value, issue_id, job_id, state_sink=None):
        captured["state"] = input_value
        return None, False, None

    ex = IssueExecutor.__new__(IssueExecutor)
    ex._jobs = MagicMock(get=AsyncMock(return_value={"trace_id": None}), update=AsyncMock())
    ex._issues = MagicMock(update=AsyncMock(), update_parent_status=AsyncMock())
    ex._db = None
    ex._audit_log_path = None
    ex._config = SimpleNamespace(environment_secret_key="k", pr_dedup_enabled=False)
    ex._env_repo = None
    ex._github_token = ""
    ex._execute_workflow_stream = _stream  # type: ignore[method-assign]
    ex._cleanup_sandbox = AsyncMock()  # type: ignore[method-assign]
    ex._post_tracker_outcome = AsyncMock()  # type: ignore[method-assign]
    ex._finish_credential_uses = AsyncMock()  # type: ignore[method-assign]
    if recorder is not None:
        ex._case_recorder = recorder
    return ex, captured


_ISSUE = {"title": "t", "body": "", "repo": "acme/widgets"}


@pytest.mark.asyncio
async def test_a_watcher_case_job_is_pinned_to_the_full_pipeline():
    recorder = MagicMock(case_origin=AsyncMock(return_value="watcher"))
    ex, captured = _executor(recorder)
    await ex._run_workflow("iss-1", "job-1", _ISSUE)
    recorder.case_origin.assert_awaited_once_with("iss-1")
    assert captured["state"].get("pin_full_pipeline") is True, "a watcher-origin job was not pinned"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["human", None])
async def test_other_jobs_are_not_pinned(origin):
    ex, captured = _executor(MagicMock(case_origin=AsyncMock(return_value=origin)))
    await ex._run_workflow("iss-1", "job-1", _ISSUE)
    assert "pin_full_pipeline" not in captured["state"]


@pytest.mark.asyncio
async def test_a_ledger_failure_does_not_stop_the_job():
    ex, captured = _executor(MagicMock(case_origin=AsyncMock(side_effect=RuntimeError("ledger down"))))
    await ex._run_workflow("iss-1", "job-1", _ISSUE)
    assert "state" in captured and "pin_full_pipeline" not in captured["state"]


@pytest.mark.asyncio
async def test_an_issue_less_job_never_asks_the_ledger():
    recorder = MagicMock(case_origin=AsyncMock(return_value="watcher"))
    ex, captured = _executor(recorder)
    await ex._run_workflow(None, "job-1", _ISSUE)
    recorder.case_origin.assert_not_awaited()
