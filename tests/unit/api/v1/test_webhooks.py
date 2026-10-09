import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config


@pytest.fixture
def app():
    config = Embry0Config(
        _env_file=None, auth_dev_mode=True, webhook_dev_mode=True, github_webhook_secret="test-secret"
    )
    app = create_app(config)
    # Mock required state
    mock_issues_repo = MagicMock()
    mock_github_sync = MagicMock()
    mock_github_sync.handle_webhook_event = AsyncMock(return_value={"status": "ignored"})
    app.state.issues_repo = mock_issues_repo
    app.state.inputs_repo = MagicMock()
    app.state.github_sync = mock_github_sync
    app.state.issue_executor = MagicMock()
    return app


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_webhook_valid_signature(app):
    payload = json.dumps({"action": "labeled", "issue": {"number": 1}}).encode()
    sig = _sign(payload, "test-secret")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhook",
            content=payload,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_webhook_invalid_signature(app):
    payload = b'{"action": "labeled"}'
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhook",
            content=payload,
            headers={
                "X-Hub-Signature-256": "sha256=invalid",
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_no_secret_dev_mode_accepts(app):
    """DEV_MODE=true and no secret configured: accept (smee.io local dev flow)."""
    app.state.config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True, github_webhook_secret="")
    payload = json.dumps({"action": "opened", "issue": {"number": 1}, "repository": {"full_name": "o/r"}}).encode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhook",
            content=payload,
            headers={
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_webhook_smee_envelope_unwrapped(app):
    """Smee.io wraps the real payload in {'payload': '<json-string>'}. Handler must unwrap."""
    # Dev-mode app with NO secret, simulating local smee flow
    app.state.config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True, github_webhook_secret="")

    real_payload = {"action": "labeled", "issue": {"number": 42}, "repository": {"full_name": "o/r"}}
    smee_envelope = {"payload": json.dumps(real_payload)}
    body = json.dumps(smee_envelope).encode()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200
    # github_sync.handle_webhook_event should have been called with the UNWRAPPED payload
    app.state.github_sync.handle_webhook_event.assert_awaited_once()
    payload_arg, ctx = app.state.github_sync.handle_webhook_event.await_args.args
    assert ctx.action == "labeled"
    assert payload_arg["issue"]["number"] == 42


@pytest.mark.asyncio
async def test_webhook_dev_mode_no_secret_accepts_unsigned(app):
    """DEV_MODE=true with no secret accepts unsigned webhooks (smee flow)."""
    app.state.config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True, github_webhook_secret="")
    payload = json.dumps({"action": "opened", "issue": {"number": 1}, "repository": {"full_name": "o/r"}}).encode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhook",
            content=payload,
            headers={
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_webhook_prod_mode_no_secret_rejects_with_503(app):
    """Without dev_mode and no secret, webhook is rejected with 503."""
    app.state.config = Embry0Config(
        _env_file=None, auth_dev_mode=False, webhook_dev_mode=False, github_webhook_secret=""
    )
    payload = b'{"action": "opened"}'
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhook",
            content=payload,
            headers={
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_webhook_non_envelope_payload_field_preserved(app):
    """A real GitHub payload whose top-level 'payload' is not JSON must pass through unchanged.

    Regression guard: the smee envelope unwrap silently swallows JSONDecodeError, which is
    the intended fallback behavior. Without this test a refactor that changes the fallback to
    raise 400 would go unnoticed.
    """
    app.state.config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True, github_webhook_secret="")
    real = {
        "action": "labeled",
        "payload": "not-json-at-all",  # looks like a smee envelope field but isn't
        "issue": {"number": 99},
        "repository": {"full_name": "o/r"},
    }
    body = json.dumps(real).encode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200
    # Handler received the ORIGINAL payload (not an exception, not a rewrite)
    app.state.github_sync.handle_webhook_event.assert_awaited_once()
    payload_arg, ctx = app.state.github_sync.handle_webhook_event.await_args.args
    assert ctx.action == "labeled"
    assert payload_arg["issue"]["number"] == 99
    assert payload_arg["payload"] == "not-json-at-all"  # original field survived


@pytest.mark.asyncio
async def test_webhook_does_not_recursively_unwrap_nested_envelopes(app):
    """Handler unwraps smee envelope exactly once.

    If a payload contains a literal nested envelope (rare; not produced by smee),
    the inner envelope must be passed through to downstream handlers rather than
    silently recursively unwrapped — preventing unexpected attack surface if a
    crafted payload is ever delivered via an unsigned route.
    """
    app.state.config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True, github_webhook_secret="")

    inner_payload = {"action": "opened", "issue": {"number": 1}, "repository": {"full_name": "o/r"}}
    # A single envelope whose inner "payload" string is itself a valid envelope
    nested_inner = {"payload": json.dumps(inner_payload)}
    outer_envelope = {"payload": json.dumps(nested_inner)}
    body = json.dumps(outer_envelope).encode()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
    # The handler unwrapped ONCE — downstream sees the intermediate envelope,
    # which has no "action" key, so the handler returns "ignored" (not 200 success
    # via the real payload).
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json.get("status") == "ignored", (
        f"Double-unwrap would have delivered the real payload. Got: {body_json}"
    )


# ---- RAV-1092: tagged PR comments route to the comment-dispatch service -----


@pytest.mark.asyncio
async def test_tagged_pr_comment_routes_to_comment_dispatch(app):
    dispatch = MagicMock()
    dispatch.handle_pr_comment = AsyncMock(
        return_value={"dispatched": True, "job_id": "job-x", "pr": 418, "branch": "b", "adopted_session_from": None}
    )
    app.state.comment_dispatch = dispatch

    payload = json.dumps(
        {
            "action": "created",
            "issue": {"number": 418, "pull_request": {"url": "x"}},
            "comment": {"id": 1, "body": "@embry0 fix it", "author_association": "MEMBER", "user": {"login": "t"}},
            "repository": {"full_name": "o/r"},
        }
    ).encode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhook",
            content=payload,
            headers={
                "X-Hub-Signature-256": _sign(payload, "test-secret"),
                "X-GitHub-Event": "issue_comment",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["action"] == "comment_dispatch"
    dispatch.handle_pr_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_undispatched_pr_comment_falls_through_to_directives(app):
    dispatch = MagicMock()
    dispatch.handle_pr_comment = AsyncMock(return_value={"dispatched": False, "reason": "no_tag"})
    app.state.comment_dispatch = dispatch

    payload = json.dumps(
        {
            "action": "created",
            "issue": {"number": 418, "pull_request": {"url": "x"}},
            "comment": {"id": 2, "body": "plain comment", "author_association": "MEMBER", "user": {"login": "t"}},
            "repository": {"full_name": "o/r"},
        }
    ).encode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhook",
            content=payload,
            headers={
                "X-Hub-Signature-256": _sign(payload, "test-secret"),
                "X-GitHub-Event": "issue_comment",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "no directives"


# ---------------------------------------------------------------------------
# trigger-label resolution: DB row over env (#157)
# ---------------------------------------------------------------------------


def _issues_payload() -> bytes:
    return json.dumps(
        {
            "action": "labeled",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 1, "labels": [{"name": "ship-it"}]},
        }
    ).encode()


async def _post_issues(app) -> None:
    payload = _issues_payload()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/v1/webhook",
            content=payload,
            headers={
                "X-Hub-Signature-256": _sign(payload, "test-secret"),
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )


@pytest.mark.asyncio
async def test_webhook_uses_db_trigger_labels_over_env(app, monkeypatch):
    """The dashboard-editable column used to be write-only on this path."""
    monkeypatch.setenv("TRIGGER_LABELS", "from-env")
    repo = MagicMock()
    repo.get_trigger_labels = AsyncMock(return_value=["ship-it"])
    app.state.integration_repo = repo

    await _post_issues(app)

    _, ctx = app.state.github_sync.handle_webhook_event.await_args.args
    assert ctx.trigger_labels == {"ship-it"}


@pytest.mark.asyncio
async def test_webhook_falls_back_to_env_without_the_repo(app, monkeypatch):
    """A partially-wired app.state must not break the webhook."""
    app.state.config = Embry0Config(
        _env_file=None,
        auth_dev_mode=True,
        webhook_dev_mode=True,
        github_webhook_secret="",
        trigger_labels="ship-it,other",
    )
    if hasattr(app.state, "integration_repo"):
        delattr(app.state, "integration_repo")

    await _post_issues(app)

    _, ctx = app.state.github_sync.handle_webhook_event.await_args.args
    assert ctx.trigger_labels == {"ship-it", "other"}


@pytest.mark.asyncio
async def test_webhook_falls_back_to_env_when_the_db_read_fails(app, monkeypatch):
    """A DB blip must not silently disable every trigger."""
    app.state.config = Embry0Config(
        _env_file=None,
        auth_dev_mode=True,
        webhook_dev_mode=True,
        github_webhook_secret="",
        trigger_labels="ship-it",
    )
    repo = MagicMock()
    repo.get_trigger_labels = AsyncMock(side_effect=RuntimeError("pool exhausted"))
    app.state.integration_repo = repo

    await _post_issues(app)

    _, ctx = app.state.github_sync.handle_webhook_event.await_args.args
    assert ctx.trigger_labels == {"ship-it"}
