"""start_job must persist and thread the per-agent model override (EMB-45).

POST /jobs {"pipeline": "qa", "agent_models": {...}} used to drop the override
silently — the QA branch of the handler never forwarded it and _run_qa_workflow
never surfaced it on state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.services.issue_executor import IssueExecutor


def _executor() -> IssueExecutor:
    jobs_repo = MagicMock()
    jobs_repo.create = AsyncMock(return_value="job-test-1")
    return IssueExecutor(
        issues_repo=MagicMock(),
        jobs_repo=jobs_repo,
        traces_repo=MagicMock(),
        workflow_registry=MagicMock(),
        database_url="postgresql://unused",
    )


@pytest.mark.asyncio
async def test_start_job_persists_and_threads_agent_models(monkeypatch):
    ex = _executor()
    tracked: dict = {}

    def fake_track(coro, **kw):
        # Don't run the workflow — just record that it was scheduled and close
        # the coroutine so asyncio doesn't warn about it never being awaited.
        tracked["scheduled"] = True
        coro.close()

    monkeypatch.setattr(ex, "_track_task", fake_track)
    run_qa = AsyncMock()
    monkeypatch.setattr(ex, "_run_qa_workflow", run_qa)

    job_id = await ex.start_job(
        repo="o/r",
        branch="main",
        pipeline="qa",
        qa_overrides={},
        agent_models={"qa": "grok-4.5"},
    )
    assert job_id == "job-test-1"

    create_kwargs = ex._jobs.create.call_args.kwargs
    assert create_kwargs["pipeline_config"]["agent_models_override"] == {"qa": "grok-4.5"}

    run_qa.assert_called_once()
    assert run_qa.call_args.kwargs["agent_models"] == {"qa": "grok-4.5"}
    assert tracked["scheduled"] is True


@pytest.mark.asyncio
async def test_start_job_without_agent_models_omits_override(monkeypatch):
    ex = _executor()
    monkeypatch.setattr(ex, "_track_task", lambda coro, **kw: coro.close())
    monkeypatch.setattr(ex, "_run_qa_workflow", AsyncMock())

    await ex.start_job(repo="o/r", branch="main", pipeline="qa", qa_overrides={})

    create_kwargs = ex._jobs.create.call_args.kwargs
    assert "agent_models_override" not in create_kwargs["pipeline_config"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("overrides", "expected"), [({}, None), ({"sandbox_profile": "qa-custom"}, "qa-custom")])
async def test_start_job_records_no_placeholder_profile(monkeypatch, overrides, expected):
    """#537: a QA job row used to say qa-jvm whatever the run used."""
    ex = _executor()
    monkeypatch.setattr(ex, "_track_task", lambda coro, **kw: coro.close())
    monkeypatch.setattr(ex, "_run_qa_workflow", AsyncMock())

    await ex.start_job(repo="o/r", branch="main", pipeline="qa", qa_overrides=overrides)

    got = ex._jobs.create.call_args.kwargs["sandbox_profile"]
    assert got == expected, f"jobs.sandbox_profile at creation must be the caller's override or nothing, got {got!r}"
