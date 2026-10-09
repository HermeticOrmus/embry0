"""Unit tests for the issues API — focused on the answer-input endpoint."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config


@pytest.fixture
def app():
    config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True)
    app = create_app(config)

    mock_issues = MagicMock()
    mock_issues.get = AsyncMock(return_value={"id": "iss-001", "status": "awaiting_input"})
    app.state.issues_repo = mock_issues

    _input_row = {
        "id": "inp-001",
        "issue_id": "iss-001",
        "job_id": "job-001",
        "asking_node": "developer",
        "question": "Should I proceed?",
        "importance": "blocking",
        "auto_answer": None,
        "answer": None,
        "answered_by": None,
        "status": "pending",
        "created_at": datetime(2024, 1, 1),
        "answered_at": None,
    }
    _answered_row = {**_input_row, "answer": "yes", "answered_by": "user", "status": "answered"}

    mock_inputs = MagicMock()
    mock_inputs.get = AsyncMock(return_value=_input_row)
    mock_inputs.answer = AsyncMock()
    mock_inputs.count_pending_blocking = AsyncMock(return_value=1)
    mock_inputs.get = AsyncMock(side_effect=[_input_row, _answered_row])
    app.state.inputs_repo = mock_inputs

    mock_executor = MagicMock()
    mock_executor.resume_or_execute = AsyncMock()
    app.state.issue_executor = mock_executor

    return app


_CSRF_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def client_with_pending_input(app):
    """AsyncClient pre-wired with a known issue and pending input."""
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return client, "iss-001", "inp-001"


@pytest.mark.asyncio
async def test_answer_input_rejects_extra_fields(client_with_pending_input):
    """AnswerInputRequest extra='forbid' must reject unexpected fields."""
    client, issue_id, input_id = client_with_pending_input
    resp = await client.post(
        f"/api/v1/issues/{issue_id}/inputs/{input_id}/answer",
        json={"answer": "yes", "extra_field": "boom"},
        headers=_CSRF_HEADERS,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_answer_input_rejects_empty_answer(client_with_pending_input):
    """min_length=1 must reject an empty string answer."""
    client, issue_id, input_id = client_with_pending_input
    resp = await client.post(
        f"/api/v1/issues/{issue_id}/inputs/{input_id}/answer",
        json={"answer": ""},
        headers=_CSRF_HEADERS,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_answer_input_accepts_valid_answer(client_with_pending_input):
    """Happy path — valid answer returns 200 with the updated input."""
    client, issue_id, input_id = client_with_pending_input
    resp = await client.post(
        f"/api/v1/issues/{issue_id}/inputs/{input_id}/answer",
        json={"answer": "yes"},
        headers=_CSRF_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["answer"] == "yes"


# ---------------------------------------------------------------------------
# notification_channels round-trip — Plan A Task 2.
#
# These use the real-DB ``api_client`` fixture from
# ``tests/unit/api/conftest.py`` (cascades into this v1/ subdirectory) so the
# round-trip exercises the schema → repository → Postgres jsonb column path.
# auto_triage=False keeps the executor inert.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_issue_with_notification_channels(api_client):
    payload = {
        "repo": "owner/repo",
        "title": "Test issue with channels",
        "body": "...",
        "auto_triage": False,
        "notification_channels": ["dashboard", "telegram", "github"],
    }
    r = await api_client.post("/api/v1/issues", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["notification_channels"] == ["dashboard", "telegram", "github"]

    # GET round-trip — value persisted to Postgres and re-read.
    r2 = await api_client.get(f"/api/v1/issues/{body['id']}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["notification_channels"] == ["dashboard", "telegram", "github"]


@pytest.mark.asyncio
async def test_create_issue_default_notification_channels(api_client):
    payload = {
        "repo": "owner/repo",
        "title": "Test default",
        "body": "...",
        "auto_triage": False,
    }
    r = await api_client.post("/api/v1/issues", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["notification_channels"] == ["dashboard"]


@pytest.mark.asyncio
async def test_create_issue_rejects_unknown_notification_channel(api_client):
    payload = {
        "repo": "owner/repo",
        "title": "Test reject",
        "body": "...",
        "auto_triage": False,
        "notification_channels": ["dashboard", "carrier-pigeon"],
    }
    r = await api_client.post("/api/v1/issues", json=payload)
    assert r.status_code == 422
    assert "carrier-pigeon" in r.text or "channel" in r.text.lower()


@pytest.mark.asyncio
async def test_create_issue_rejects_duplicate_notification_channels(api_client):
    r = await api_client.post(
        "/api/v1/issues",
        json={
            "repo": "owner/repo",
            "title": "T",
            "body": "...",
            "notification_channels": ["dashboard", "dashboard"],
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 422
    assert "duplicate" in r.text.lower() or "unique" in r.text.lower()


@pytest.mark.asyncio
async def test_update_issue_notification_channels(api_client):
    # Create with default (no channels in payload → server defaults to ["dashboard"]).
    create_r = await api_client.post(
        "/api/v1/issues",
        json={
            "repo": "owner/repo",
            "title": "T",
            "body": "...",
            "auto_triage": False,
        },
    )
    assert create_r.status_code == 201, create_r.text
    iid = create_r.json()["id"]
    assert create_r.json()["notification_channels"] == ["dashboard"]

    # Update via the canonical update endpoint (PUT — mirrors agents/sandbox-profiles).
    update_r = await api_client.put(
        f"/api/v1/issues/{iid}",
        json={"notification_channels": ["dashboard", "telegram"]},
    )
    assert update_r.status_code == 200, update_r.text
    assert update_r.json()["notification_channels"] == ["dashboard", "telegram"]

    # GET round-trip — confirm persistence.
    get_r = await api_client.get(f"/api/v1/issues/{iid}")
    assert get_r.json()["notification_channels"] == ["dashboard", "telegram"]


# ---------------------------------------------------------------------------
# #284: POST /issues with auto_triage reports what happened to the dispatch.
# A 201 used to look identical whether a job started or execute() raised.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_issue_reports_the_dispatched_job(api_client, monkeypatch):
    executor = api_client.app.state.issue_executor
    monkeypatch.setattr(executor, "execute", AsyncMock(return_value="job-abc123"))

    r = await api_client.post("/api/v1/issues", json={"repo": "owner/repo", "title": "T", "auto_triage": True})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["job_id"] == "job-abc123"
    assert body["dispatch_error"] is None and body["dispatch_error_code"] is None
    assert body["status"] == "triaging"


@pytest.mark.asyncio
async def test_create_issue_surfaces_a_failed_dispatch(api_client, monkeypatch):
    executor = api_client.app.state.issue_executor
    monkeypatch.setattr(executor, "execute", AsyncMock(side_effect=RuntimeError("sandbox backend down")))

    r = await api_client.post("/api/v1/issues", json={"repo": "owner/repo", "title": "T", "auto_triage": True})

    # Still 201: the issue exists, and a non-2xx would invite a retry that
    # creates it twice. The failure rides the body instead of the log.
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["job_id"] is None
    assert body["dispatch_error_code"] == "ERR_DISPATCH_FAILED"
    # A stable code and a safe sentence — the exception text stays in the log.
    assert "sandbox backend down" not in r.text
    assert "no job was started" in body["dispatch_error"] and body["id"] in body["dispatch_error"]
    assert body["status"] == "open"  # reverted, so POST /issues/{id}/triage can retry


@pytest.mark.asyncio
async def test_create_issue_without_auto_triage_reports_no_dispatch(api_client):
    r = await api_client.post("/api/v1/issues", json={"repo": "owner/repo", "title": "T", "auto_triage": False})

    assert r.status_code == 201, r.text
    assert r.json()["job_id"] is None and r.json()["dispatch_error"] is None


# ---- POST /issues/{id}/triage — the same silent failure, the same contract ----


async def _open_issue(api_client) -> str:
    r = await api_client.post("/api/v1/issues", json={"repo": "owner/repo", "title": "T", "auto_triage": False})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_triage_reports_the_dispatched_job(api_client, monkeypatch):
    issue_id = await _open_issue(api_client)
    monkeypatch.setattr(api_client.app.state.issue_executor, "execute", AsyncMock(return_value="job-xyz"))

    r = await api_client.post(f"/api/v1/issues/{issue_id}/triage")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == "job-xyz" and body["dispatch_error"] is None and body["status"] == "triaging"


@pytest.mark.asyncio
async def test_triage_surfaces_a_failed_dispatch(api_client, monkeypatch):
    issue_id = await _open_issue(api_client)
    monkeypatch.setattr(
        api_client.app.state.issue_executor, "execute", AsyncMock(side_effect=RuntimeError("token=hunter2 rejected"))
    )

    r = await api_client.post(f"/api/v1/issues/{issue_id}/triage")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] is None and body["dispatch_error_code"] == "ERR_DISPATCH_FAILED"
    assert "hunter2" not in r.text
    assert body["status"] == "open"


# ---- credential registry: cancelling an issue closes its jobs' use ledger ----


def _delete_issue_request(credentials_repo):
    request = MagicMock()
    request.app.state.credentials_repo = credentials_repo
    request.app.state.config = MagicMock(audit_log_path=None)
    request.app.state.db = MagicMock()
    return request


def _delete_issue_repos():
    issues = MagicMock(
        get=AsyncMock(return_value={"id": "iss-1", "status": "open"}),
        get_children=AsyncMock(return_value=[]),
        update=AsyncMock(),
    )
    order: list[tuple] = []

    async def _update(job_id, **kw):
        order.append(("update", job_id, kw.get("status")))

    jobs = MagicMock(
        list_all=AsyncMock(
            return_value=([{"job_id": "job-1", "status": "running"}, {"job_id": "job-2", "status": "completed"}], 2)
        ),
        update=AsyncMock(side_effect=_update),
    )
    return issues, jobs, order


@pytest.mark.asyncio
async def test_delete_issue_stamps_cancelled_credential_uses(monkeypatch):
    import embry0.api.v1.issues as issues_mod

    monkeypatch.setattr(issues_mod, "emit_audit", AsyncMock())
    monkeypatch.setattr(issues_mod, "_actor", lambda request: "tester")
    issues, jobs, order = _delete_issue_repos()

    async def _finish(job_id, outcome):
        order.append(("finish", job_id, outcome))

    repo = MagicMock(finish_uses=AsyncMock(side_effect=_finish))

    await issues_mod.delete_issue("iss-1", _delete_issue_request(repo), issues=issues, jobs=jobs)

    # Only the job the route actually cancelled is stamped, after its write.
    assert order == [("update", "job-1", "cancelled"), ("finish", "job-1", "cancelled")], order


@pytest.mark.asyncio
async def test_delete_issue_survives_credential_ledger_failure(monkeypatch):
    import embry0.api.v1.issues as issues_mod

    monkeypatch.setattr(issues_mod, "emit_audit", AsyncMock())
    monkeypatch.setattr(issues_mod, "_actor", lambda request: "tester")
    issues, jobs, _ = _delete_issue_repos()
    repo = MagicMock(finish_uses=AsyncMock(side_effect=RuntimeError("db down")))

    await issues_mod.delete_issue("iss-1", _delete_issue_request(repo), issues=issues, jobs=jobs)

    repo.finish_uses.assert_awaited_once_with("job-1", "cancelled")
    issues.update.assert_any_await("iss-1", status="cancelled")


@pytest.mark.asyncio
async def test_delete_issue_without_credentials_repo(monkeypatch):
    import embry0.api.v1.issues as issues_mod

    monkeypatch.setattr(issues_mod, "emit_audit", AsyncMock())
    monkeypatch.setattr(issues_mod, "_actor", lambda request: "tester")
    issues, jobs, order = _delete_issue_repos()
    request = _delete_issue_request(None)

    await issues_mod.delete_issue("iss-1", request, issues=issues, jobs=jobs)

    assert order == [("update", "job-1", "cancelled")]
