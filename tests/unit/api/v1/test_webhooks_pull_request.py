"""pull_request webhook → merge sync (#279, #280): the handler is a thin adapter."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config
from embry0.services.merge_sync.facts import DeliveryState
from embry0.services.merge_sync.recorder import RecordResult
from embry0.storage.repositories.issues import IssuesRepository
from embry0.storage.repositories.jobs import JobsRepository

URL = "https://github.com/o/r/pull/1"


def _payload(action: str, **pr: Any) -> bytes:
    body = {
        "html_url": URL,
        "number": 1,
        "state": "open",
        "merged": False,
        "created_at": "2026-08-01T10:00:00Z",
        "updated_at": "2026-08-01T10:00:00Z",
        "head": {"ref": "embry0/unknown-fix-bug", "sha": "abc", "repo": {"full_name": "o/r"}},
        "base": {"repo": {"full_name": "o/r"}},
        **pr,
    }
    return json.dumps({"action": action, "repository": {"full_name": "o/r"}, "pull_request": body}).encode()


@pytest.fixture
def app():
    return create_app(Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True, github_webhook_secret=""))


async def _post(app, body: bytes, delivery: str | None = "d-1"):
    headers = {"X-GitHub-Event": "pull_request", "Content-Type": "application/json"}
    if delivery:
        headers["X-GitHub-Delivery"] = delivery
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/v1/webhook", content=body, headers=headers)


class _Recorder:
    def __init__(self, matched: bool) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self._matched = matched

    async def record(self, fact, **kwargs):
        self.calls.append((fact, kwargs))
        return RecordResult(pr_url=fact.url, state=fact.state.value, matched_jobs=["job-1"] if self._matched else [])


@pytest.mark.asyncio
async def test_a_merge_is_handed_to_merge_sync_with_forge_time_and_delivery_id(app):
    app.state.delivery_recorder = recorder = _Recorder(matched=True)
    body = _payload(
        "closed", state="closed", merged=True, merged_at="2026-08-02T09:00:00Z", closed_at="2026-08-02T09:00:00Z"
    )

    resp = await _post(app, body, delivery="7f3a")

    assert resp.json() == {"status": "accepted", "action": "pr_closed"}
    fact, kwargs = recorder.calls[0]
    assert (fact.url, fact.state, fact.at) == (URL, DeliveryState.MERGED, datetime(2026, 8, 2, 9, 0, tzinfo=UTC))
    assert kwargs == {"source": "webhook", "event_key": "github:7f3a"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["opened", "reopened", "synchronize", "closed"])
async def test_every_state_bearing_action_is_recorded(app, action):
    app.state.delivery_recorder = recorder = _Recorder(matched=True)
    assert (await _post(app, _payload(action))).json()["status"] == "accepted"
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["labeled", "edited", "review_requested"])
async def test_other_actions_are_ignored_without_touching_anything(app, action):
    app.state.delivery_recorder = recorder = _Recorder(matched=True)
    assert (await _post(app, _payload(action))).json()["status"] == "ignored"
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_pr_that_belongs_to_no_job_is_ignored(app):
    app.state.delivery_recorder = _Recorder(matched=False)
    assert (await _post(app, _payload("opened"))).json() == {
        "status": "ignored",
        "event": "pull_request",
        "action": "opened",
    }


@pytest.mark.asyncio
async def test_a_payload_without_a_pr_url_is_ignored(app):
    app.state.delivery_recorder = recorder = _Recorder(matched=True)
    assert (await _post(app, _payload("opened", html_url=""))).json()["status"] == "ignored"
    assert recorder.calls == []


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_end_to_end_a_failed_jobs_merged_pr_is_recorded_without_a_wired_recorder(app, db_with_migrations):
    """The 2026-09-20 case: `embry0/unknown-*` branch, job `failed`, PR merged —
    through the real handler, the fallback recorder and Postgres."""
    db = db_with_migrations
    app.state.db = db
    app.state.issues_repo = IssuesRepository(db)
    job_id = await JobsRepository(db).create(repo="o/r", task="Fix bug")
    await db.execute("UPDATE jobs SET status = 'failed', created_at = '2026-08-01T09:00:00Z' WHERE job_id = $1", job_id)
    merged = _payload(
        "closed", state="closed", merged=True, merged_at="2026-08-02T09:00:00Z", closed_at="2026-08-02T09:00:00Z"
    )

    for delivery in ("d-1", "d-1", "d-2"):  # delivered, redelivered, delivered again under a new id
        assert (await _post(app, merged, delivery=delivery)).json() == {"status": "accepted", "action": "pr_closed"}
    # …and a stale `opened` arriving after the merge
    assert (await _post(app, _payload("opened"), delivery="d-0")).status_code == 200

    row = await db.fetchrow(
        "SELECT status, delivery_state, delivery_state_at, pr_url FROM jobs WHERE job_id = $1", job_id
    )
    assert (row["status"], row["delivery_state"], row["pr_url"]) == ("failed", "merged", URL)
    assert row["delivery_state_at"] == datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
