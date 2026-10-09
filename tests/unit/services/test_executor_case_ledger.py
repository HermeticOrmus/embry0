"""The job lifecycle reports to the healing-case ledger (#278 part 2).

``IssueExecutor.execute`` is where every dispatch ends (tracker webhooks, the
Linear reconciler, a dashboard retry, an auto-answered re-triage), and
``_handle_workflow_result`` is where a job's PR url is first persisted.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embry0.services.issue_executor import IssueExecutor

_ISSUE = {
    "id": "iss-1",
    "repo": "o/r",
    "title": "[ENG-1] t",
    "body": None,
    "tracker": "linear",
    "tracker_key": "ENG-1",
    "status": "triaging",
}


def _dispatching_executor(recorder):
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._background_tasks = set()
    ex._tasks_by_job = {}
    ex._event_bus = None
    ex._audit_log_path = None
    ex._db = MagicMock()
    ex._issues = MagicMock(get=AsyncMock(return_value=_ISSUE))
    ex._jobs = MagicMock(list_all=AsyncMock(return_value=([], 0)), create=AsyncMock(return_value="job-abc"))
    ex._track_task = lambda coro, **_: coro.close()  # type: ignore[method-assign]
    if recorder is not None:
        ex._case_recorder = recorder
    return ex


@pytest.mark.asyncio
async def test_execute_reports_the_new_job_with_the_issue_row():
    recorder = MagicMock(job_dispatched=AsyncMock(return_value="hc-1"))
    ex = _dispatching_executor(recorder)
    with patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()):
        assert await ex.execute("iss-1") == "job-abc"
    recorder.job_dispatched.assert_awaited_once_with(_ISSUE, "job-abc")


@pytest.mark.asyncio
async def test_execute_works_without_a_recorder():
    ex = _dispatching_executor(None)  # built via __new__, as many tests do
    with patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()):
        assert await ex.execute("iss-1") == "job-abc"


STARTED = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)


def _finishing_executor(recorder):
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._jobs = MagicMock(
        update=AsyncMock(),
        # created_at is the bound the ledger falls back to for opened_at.
        get=AsyncMock(return_value={"repo": "o/r", "task": "t", "created_at": STARTED}),
    )
    ex._issues = MagicMock(update=AsyncMock(), update_parent_status=AsyncMock())
    ex._db = None
    ex._audit_log_path = None
    ex._database_url = "postgresql://unused"
    ex._webhook_notifier = None
    ex._case_recorder = recorder
    return ex


async def _finish(ex, result):
    with (
        patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()),
        patch("embry0.orchestration.checkpoint.purge_thread", new=AsyncMock()),
    ):
        await ex._handle_workflow_result("iss-1", "job-1", result)


@pytest.mark.asyncio
async def test_a_finished_job_reports_its_pr_after_the_url_is_persisted():
    order: list[str] = []
    recorder = MagicMock(pr_opened=AsyncMock(side_effect=lambda **_: order.append("ledger")))
    ex = _finishing_executor(recorder)
    ex._jobs.update = AsyncMock(side_effect=lambda *_a, **_k: order.append("job_row"))
    url = "https://github.com/o/r/pull/5"
    await _finish(ex, {"pr_url": url, "agent_outputs": [{"agent_type": "review", "is_error": False}]})
    recorder.pr_opened.assert_awaited_once_with(issue_id="iss-1", job_id="job-1", pr_url=url, job_started_at=STARTED)
    assert order[:2] == ["job_row", "ledger"]


@pytest.mark.asyncio
async def test_a_failed_job_that_opened_a_pr_still_reports_it():
    recorder = MagicMock(pr_opened=AsyncMock())
    ex = _finishing_executor(recorder)
    url = "https://github.com/o/r/pull/5"
    await _finish(ex, {"pr_url": url, "agent_outputs": [{"agent_type": "review", "is_error": True}]})
    assert ex._jobs.update.await_args_list[0].kwargs["status"] == "failed"
    recorder.pr_opened.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_job_without_a_pr_reports_none():
    recorder = MagicMock(pr_opened=AsyncMock(), job_abandoned=AsyncMock())
    await _finish(_finishing_executor(recorder), {"agent_outputs": [{"agent_type": "analysis", "is_error": False}]})
    recorder.pr_opened.assert_not_awaited()
    recorder.job_abandoned.assert_not_awaited()


# ---- #410: an abandoned attempt is the other terminal shape ---------------------

_ABANDONED = {"current_stage": "abandoned", "errors": ["Abandoned after 0 retries"], "agent_outputs": []}


@pytest.mark.asyncio
async def test_an_abandoned_job_with_no_pr_closes_its_case():
    # #410: a person said "abandon" and nothing was pushed. Without this the
    # case stayed at `accepted` with no job running and nothing could move it,
    # so Embry0HealingAcceptedCaseNotDispatched paged forever on a decision
    # that had already been made.
    recorder = MagicMock(pr_opened=AsyncMock(), job_abandoned=AsyncMock())
    ex = _finishing_executor(recorder)

    await _finish(ex, dict(_ABANDONED))

    assert ex._jobs.update.await_args_list[0].kwargs["error_code"] == "ERR_ABANDONED"
    recorder.job_abandoned.assert_awaited_once()
    kwargs = recorder.job_abandoned.await_args.kwargs
    assert (kwargs["issue_id"], kwargs["job_id"]) == ("iss-1", "job-1")
    assert "Abandoned" in (kwargs["note"] or "")
    recorder.pr_opened.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_abandoned_job_that_did_open_a_pr_reports_the_pr_instead():
    # Abandoning with a PR open is a real choice (the retry pause offers
    # merge_as_is OR abandon there). The change request is live, so the case
    # follows the PR and must not be closed.
    recorder = MagicMock(pr_opened=AsyncMock(), job_abandoned=AsyncMock())
    await _finish(_finishing_executor(recorder), {**_ABANDONED, "pr_url": "https://github.com/o/r/pull/5"})
    recorder.pr_opened.assert_awaited_once()
    recorder.job_abandoned.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_job_that_failed_some_other_way_leaves_its_case_open():
    # Narrow by design: a crash, a turn cap or a malformed triage is a job to
    # retry, and the acceptance still stands. Closing those would silence the
    # very alert that says "a human accepted this and embry0 never delivered".
    recorder = MagicMock(pr_opened=AsyncMock(), job_abandoned=AsyncMock())
    ex = _finishing_executor(recorder)

    await _finish(ex, {"current_stage": "developer", "errors": ["sandbox exploded"], "agent_outputs": []})

    assert ex._jobs.update.await_args_list[0].kwargs["status"] == "failed"
    assert ex._jobs.update.await_args_list[0].kwargs["error_code"] != "ERR_ABANDONED"
    recorder.job_abandoned.assert_not_awaited()


# ---- the ledger is duck-typed at these two call sites (review of #340, nit 6) --


@pytest.mark.asyncio
async def test_a_recorder_that_raises_cannot_stop_a_dispatch():
    # CaseRecorder cannot raise, but nothing at this boundary enforces that:
    # the attribute is injected and untyped, so the call site guards too.
    recorder = MagicMock(job_dispatched=AsyncMock(side_effect=RuntimeError("wrong object injected")))
    ex = _dispatching_executor(recorder)
    with patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()):
        assert await ex.execute("iss-1") == "job-abc"


@pytest.mark.asyncio
async def test_a_recorder_that_raises_cannot_strand_a_finished_job():
    recorder = MagicMock(pr_opened=AsyncMock(side_effect=RuntimeError("wrong object injected")))
    ex = _finishing_executor(recorder)
    await _finish(ex, {"pr_url": "https://github.com/o/r/pull/5", "agent_outputs": [{"is_error": False}]})
    assert ex._jobs.update.await_args_list[0].kwargs["status"] == "completed"
    # and the rest of the terminal path still ran
    ex._issues.update.assert_awaited()
