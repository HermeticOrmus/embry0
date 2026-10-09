"""POST /api/v1/webhook/linear — signature rules + dispatch wiring (EMB-47)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config

_PAYLOAD = {"type": "Issue", "action": "create", "data": {"id": "uuid-1", "identifier": "EMB-48"}}


class _FakeLinearSync:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.trigger_labels: set[str] | None = None

    @property
    def name(self) -> str:
        return "linear"

    async def handle_webhook_event(self, payload, ctx):
        self.calls.append(payload)
        self.trigger_labels = ctx.trigger_labels
        return {"status": "accepted", "action": "dispatched"}

    async def post_issue_comment(self, ref, body) -> bool:
        return True


def _app(*, webhook_dev_mode: bool, secret: str = "", wire_sync: bool = True):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    config = Embry0Config(
        database_url="postgresql://unused",
        auth_dev_mode=True,
        webhook_dev_mode=webhook_dev_mode,
        linear_webhook_secret=secret,
    )
    app = create_app(config, lifespan_override=_noop_lifespan)
    app.state.config = config
    app.state.issues_repo = object()
    app.state.issue_executor = object()
    sync = _FakeLinearSync() if wire_sync else None
    app.state.linear_sync = sync
    return app, sync


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_dev_mode_bypasses_signature() -> None:
    app, sync = _app(webhook_dev_mode=True)
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/linear", json=_PAYLOAD)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    assert sync.calls and sync.calls[0]["type"] == "Issue"


async def test_valid_signature_accepted() -> None:
    secret = "shh-linear"
    app, sync = _app(webhook_dev_mode=False, secret=secret)
    body = json.dumps(_PAYLOAD).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    async with _client(app) as client:
        r = await client.post(
            "/api/v1/webhook/linear",
            content=body,
            headers={"Linear-Signature": sig, "Content-Type": "application/json"},
        )
    assert r.status_code == 200, r.text
    assert sync.calls


@pytest.mark.parametrize("sig", ["", "deadbeef"])
async def test_bad_signature_rejected(sig: str) -> None:
    app, _ = _app(webhook_dev_mode=False, secret="shh-linear")
    async with _client(app) as client:
        r = await client.post(
            "/api/v1/webhook/linear",
            content=json.dumps(_PAYLOAD).encode(),
            headers={"Linear-Signature": sig, "Content-Type": "application/json"},
        )
    assert r.status_code == 401


async def test_missing_secret_is_503() -> None:
    app, _ = _app(webhook_dev_mode=False, secret="")
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/linear", json=_PAYLOAD)
    assert r.status_code == 503


async def test_unconfigured_integration_ignores() -> None:
    app, _ = _app(webhook_dev_mode=True, wire_sync=False)
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/linear", json=_PAYLOAD)
    assert r.status_code == 200
    assert r.json()["reason"] == "linear integration not configured"


async def test_smee_envelope_unwrapped() -> None:
    app, sync = _app(webhook_dev_mode=True)
    envelope = {"payload": json.dumps(_PAYLOAD)}
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/linear", json=envelope)
    assert r.status_code == 200
    assert sync.calls and sync.calls[0]["type"] == "Issue"


# ---------------------------------------------------------------------------
# DB-backed credentials take effect without a restart (#157)
# ---------------------------------------------------------------------------


class _FakeIssuesRepo:
    """Reports the issue as already dispatched.

    That is the first branch in LinearSyncService.handle_webhook_event, so the
    real service short-circuits before any GraphQL call — enough to prove the
    route reached it, without touching the network.
    """

    async def get_by_tracker_ref(self, tracker, repo, tracker_key):
        return {"id": "iss-already"}


class _FakeIntegrationRepo:
    def __init__(self, api_key="", secret="", repo_map="") -> None:
        self.creds = (api_key, secret, repo_map)

    async def get_linear_credentials(self):
        return self.creds

    async def get_trigger_labels(self):
        return ["embry0"]


def _app_with_provider(*, api_key: str, secret: str, webhook_dev_mode: bool = False):
    """App whose Linear credentials come from the DB, not env."""
    from contextlib import asynccontextmanager

    from embry0.services.linear_provider import LinearSyncProvider

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    # Deliberately empty env-side Linear config: everything must come from
    # the repository, the way it does after an operator saves Settings.
    config = Embry0Config(
        database_url="postgresql://unused",
        auth_dev_mode=True,
        webhook_dev_mode=webhook_dev_mode,
        linear_api_key="",
        linear_webhook_secret="",
    )
    app = create_app(config, lifespan_override=_noop_lifespan)
    app.state.config = config
    app.state.issues_repo = _FakeIssuesRepo()
    app.state.issue_executor = object()
    app.state.linear_sync = None  # startup found no env key
    repo = _FakeIntegrationRepo(api_key=api_key, secret=secret)
    app.state.integration_repo = repo
    app.state.linear_provider = LinearSyncProvider(repo)
    return app


async def test_db_credentials_activate_linear_without_a_restart() -> None:
    """startup left linear_sync=None; the DB row must still enable the hook."""
    app = _app_with_provider(api_key="lin_key", secret="", webhook_dev_mode=True)
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/linear", json=_PAYLOAD)
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "already dispatched"  # reached the real service
    # The shared instance is refreshed for the outbound write-back paths.
    assert app.state.linear_sync is not None


async def test_db_webhook_secret_is_used_for_verification() -> None:
    app = _app_with_provider(api_key="lin_key", secret="db-secret")
    body = json.dumps(_PAYLOAD).encode()
    good = hmac.new(b"db-secret", body, hashlib.sha256).hexdigest()
    async with _client(app) as client:
        ok = await client.post("/api/v1/webhook/linear", content=body, headers={"Linear-Signature": good})
        bad = await client.post("/api/v1/webhook/linear", content=body, headers={"Linear-Signature": "deadbeef"})
    assert ok.status_code == 200, ok.text
    assert bad.status_code == 401


async def test_unconfigured_linear_still_reports_not_configured() -> None:
    app = _app_with_provider(api_key="", secret="", webhook_dev_mode=True)
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/linear", json=_PAYLOAD)
    assert r.status_code == 200
    assert r.json()["reason"] == "linear integration not configured"


async def test_dev_mode_does_not_bypass_a_configured_linear_secret() -> None:
    """Consistency fix (#159).

    The Linear route used to check `if not webhook_dev_mode` before looking at
    the secret, so dev mode skipped verification even when one was configured
    — contradicting its own docstring, which claimed "same rules as the GitHub
    hook". GitHub only ever bypassed on an EMPTY secret. Both forges now share
    that one rule.
    """
    app = _app_with_provider(api_key="lin_key", secret="db-secret", webhook_dev_mode=True)
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/linear", json=_PAYLOAD)
    assert r.status_code == 401
