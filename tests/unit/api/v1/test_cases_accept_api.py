"""POST /api/v1/cases/{id}/accept (#378) — accepting a watcher finding without
the tracker's label. Runs against the real repositories: the case, the stack
row and the issue row are real; the executor and GitHub are planted."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from embry0.services.healing.service import HealingCaseService, TrackerRef
from embry0.services.watcher_stacks import WatcherStack
from embry0.storage.repositories.watcher_stacks import WatcherStacksRepository

REPO = "acme/backend"
URL = f"https://github.com/{REPO}/issues/397"


class _FakeGitHub:
    def __init__(self, issue: dict[str, Any] | None) -> None:
        self.issue = issue
        self.asked: list[tuple[str, int]] = []

    async def get_issue(self, repo: str, number: int) -> dict[str, Any] | None:
        self.asked.append((repo, number))
        return self.issue


def _issue(state: str = "open") -> dict[str, Any]:
    return {
        "number": 397,
        "title": "[watcher] fix(orchestrator): QA check-run creation fails with 403",
        "body": "## Evidence\n\n`qa_check_run_failed` status=403",
        "html_url": URL,
        "state": state,
        "labels": [],
    }


@pytest.fixture
async def wired(api_client: AsyncClient) -> dict[str, Any]:
    app = api_client.app
    await app.state.db.execute("TRUNCATE TABLE healing_cases, issues, watcher_stacks CASCADE")
    stacks = WatcherStacksRepository(app.state.db)
    await stacks.upsert(WatcherStack(repo=REPO, logql='{compose_project="x"}', tracker="github"))
    app.state.watcher_stacks_repo = stacks
    app.state.github_issues_client = _FakeGitHub(_issue())
    app.state.issue_executor = MagicMock(execute=AsyncMock(return_value="job-1"))
    return {"app": app, "cases": app.state.healing_cases, "stacks": stacks}


async def _tracked_case(cases: HealingCaseService, repo: str = REPO, origin: str = "watcher") -> str:
    case = await cases.open_case(repo=repo, origin=origin, title="a finding", tracker=TrackerRef("github", "397", URL))
    assert case["stage"] == "tracked", case["stage"]
    return str(case["id"])


@pytest.mark.asyncio
async def test_accept_dispatches_the_draft_like_the_label_would(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    case_id = await _tracked_case(wired["cases"])
    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={"accepted_by": "Alex"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["job_id"] == "job-1" and body["status"] == "dispatched" and body["accepted_by"] == "Alex"

    # The embry0 issue row mirrors the GitHub draft, tracker ref and all —
    # the same row the label webhook would have created.
    issue = await wired["app"].state.issues_repo.get_by_tracker_ref("github", REPO, "397")
    assert issue is not None and issue["id"] == body["issue_id"]
    assert issue["tracker_url"] == URL and issue["status"] == "triaging"
    assert "qa_check_run_failed" in issue["body"]
    wired["app"].state.issue_executor.execute.assert_awaited_once_with(issue["id"], dispatched_by="Alex")
    assert wired["app"].state.github_issues_client.asked == [(REPO, 397)]


@pytest.mark.asyncio
async def test_unknown_case_is_404(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    assert (await api_client.post("/api/v1/cases/hc-nope/accept", json={})).status_code == 404


@pytest.mark.asyncio
async def test_only_a_watcher_finding_can_be_accepted(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    case_id = await _tracked_case(wired["cases"], origin="alert")
    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})
    assert r.status_code == 409 and "not a watcher finding" in r.json()["detail"]
    wired["app"].state.issue_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_gitlab_draft_keeps_the_label_path(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    """Linear drafts are dispatched here since #460 (their tests are in
    test_cases_accept_linear_api.py); GitLab is the tracker still left out."""
    case = await wired["cases"].open_case(
        repo=REPO,
        origin="watcher",
        title="x",
        tracker=TrackerRef("gitlab", "7", "https://gitlab.example.com/acme/backend/-/issues/7"),
    )
    r = await api_client.post(f"/api/v1/cases/{case['id']}/accept", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == "a gitlab draft is accepted in its tracker: add the trigger label to 7"
    wired["app"].state.issue_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stack_this_orchestrator_does_not_watch_is_refused(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    """The guarantee behind giving the chat bot's narrow key this route."""
    case_id = await _tracked_case(wired["cases"], repo="acme/other")
    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})
    assert r.status_code == 403 and "not a stack this embry0 watches" in r.json()["detail"]
    wired["app"].state.issue_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_disabled_stack_is_refused_too(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    await wired["stacks"].upsert(
        WatcherStack(repo=REPO, logql='{compose_project="x"}', tracker="github", enabled=False)
    )
    case_id = await _tracked_case(wired["cases"])
    assert (await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})).status_code == 403


@pytest.mark.asyncio
async def test_an_already_accepted_case_is_409(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    case_id = await _tracked_case(wired["cases"])
    job_id = await wired["app"].state.jobs_repo.create(repo=REPO, task="an earlier acceptance")
    await wired["cases"].attach_job(case_id, job_id, event_key=f"job:{job_id}")
    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})
    assert r.status_code == 409 and "already accepted" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_draft_closed_on_github_is_409_and_unreadable_is_502(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    case_id = await _tracked_case(wired["cases"])
    wired["app"].state.github_issues_client = _FakeGitHub(_issue(state="closed"))
    assert (await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})).status_code == 409
    wired["app"].state.github_issues_client = _FakeGitHub(None)
    assert (await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})).status_code == 502
    wired["app"].state.issue_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_accepting_twice_does_not_start_a_second_job(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    """The issue row is in `triaging` after the first accept; only `open`
    dispatches, exactly as a second label would be ignored."""
    case_id = await _tracked_case(wired["cases"])
    assert (await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})).status_code == 202
    # The planted executor never advanced the case, so the case still reads
    # `tracked` — the issue row is what refuses the second dispatch.
    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})
    assert r.status_code == 409 and "already active" in r.json()["detail"]
    assert wired["app"].state.issue_executor.execute.await_count == 1


@pytest.mark.asyncio
async def test_the_acceptance_is_audited(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    case_id = await _tracked_case(wired["cases"])
    await api_client.post(f"/api/v1/cases/{case_id}/accept", json={"accepted_by": "Alex"})
    row = await wired["app"].state.db.fetchrow(
        "SELECT details FROM audit_log WHERE action = 'case_accepted' ORDER BY created_at DESC LIMIT 1"
    )
    assert row is not None
    details = row["details"] if isinstance(row["details"], dict) else __import__("json").loads(row["details"])
    assert details["case_id"] == case_id and details["job_id"] == "job-1" and details["accepted_by"] == "Alex"
