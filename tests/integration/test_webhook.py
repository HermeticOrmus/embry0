"""Integration tests for webhook processing."""

import hashlib
import hmac
import json

import pytest
from httpx import AsyncClient


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _headers(payload: bytes, secret: str, event: str = "issues") -> dict:
    return {
        "X-Hub-Signature-256": _sign(payload, secret),
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }


async def _register(app: AsyncClient, repo: str) -> None:
    """Dispatch refuses a repo with no registry row (#360), so register it."""
    resp = await app.post(
        "/api/v1/repos", json={"repo": repo, "forge": "github"}, headers={"X-Requested-With": "XMLHttpRequest"}
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_webhook_labeled_issue_triggers(app: AsyncClient):
    """Labelling a tracked issue must dispatch a job, not just sync the column.

    This asserted only ``status == "accepted"`` before, which the old
    label-sync-only branch satisfied — so it passed against behaviour that did
    not exist. It now asserts the dispatch and the resulting job row.
    """
    await _register(app, "owner/repo")
    # First open the issue *without* the trigger label so it is merely tracked.
    open_payload = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 42, "title": "Fix auth", "body": "", "labels": [], "html_url": ""},
        }
    ).encode()
    resp = await app.post(
        "/api/v1/webhook",
        content=open_payload,
        headers=_headers(open_payload, "test-secret"),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert resp.json()["dispatched"] is False

    # Now label it with the trigger label.
    label_payload = json.dumps(
        {
            "action": "labeled",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 42, "title": "Fix auth", "labels": [{"name": "embry0"}]},
        }
    ).encode()
    resp = await app.post(
        "/api/v1/webhook",
        content=label_payload,
        headers=_headers(label_payload, "test-secret"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["dispatched"] is True

    # A job row must actually exist for the issue. GET /jobs has no issue_id
    # filter, so read the issue detail — it embeds the issue's jobs.
    issue = await app.get(f"/api/v1/issues/{body['issue_id']}")
    assert issue.status_code == 200
    assert len(issue.json()["jobs"]) == 1


@pytest.mark.asyncio
async def test_webhook_labeling_untracked_issue_adopts_it(app: AsyncClient):
    """An issue embry0 has never seen is adopted when it gets the trigger label."""
    await _register(app, "owner/repo")
    payload = json.dumps(
        {
            "action": "labeled",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 4242, "title": "Adopt me", "body": "", "labels": [{"name": "embry0"}]},
        }
    ).encode()
    resp = await app.post(
        "/api/v1/webhook",
        content=payload,
        headers=_headers(payload, "test-secret"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "issue_adopted"
    assert body["dispatched"] is True

    issue = await app.get(f"/api/v1/issues/{body['issue_id']}")
    assert issue.status_code == 200
    assert issue.json()["tracker"] == "github"
    assert issue.json()["tracker_key"] == "4242"


@pytest.mark.asyncio
async def test_webhook_non_matching_label_ignored(app: AsyncClient):
    """An untracked issue labeled with a non-trigger label is ignored."""
    payload = json.dumps(
        {
            "action": "labeled",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 99, "labels": [{"name": "bug"}]},
        }
    ).encode()
    resp = await app.post(
        "/api/v1/webhook",
        content=payload,
        headers=_headers(payload, "test-secret"),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_invalid_signature_rejected(app: AsyncClient):
    resp = await app.post(
        "/api/v1/webhook",
        content=b'{"action": "labeled"}',
        headers={
            "X-Hub-Signature-256": "sha256=invalid",
            "X-GitHub-Event": "issues",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401
