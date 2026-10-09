"""POST /api/v1/webhook/gitlab — verification + dispatch wiring (#165)."""

from __future__ import annotations

import json

from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config
from tests.fixtures.webhooks import gitlab_issue

PAYLOAD = gitlab_issue(action="update", iid=11, labels=["embry0"], previous_labels=[])


class _FakeGitLabSync:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.trigger_labels: set[str] | None = None

    @property
    def name(self) -> str:
        return "gitlab"

    async def handle_webhook_event(self, payload, ctx):
        self.calls.append(payload)
        self.trigger_labels = ctx.trigger_labels
        return {"status": "accepted", "dispatched": True}

    async def post_issue_comment(self, ref, body) -> bool:
        return True


class _FakeIntegrationRepo:
    async def get_trigger_labels(self):
        return ["embry0"]


def _app(*, secret: str = "", webhook_dev_mode: bool = False, wire: bool = True):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    config = Embry0Config(
        database_url="postgresql://unused",
        auth_dev_mode=True,
        webhook_dev_mode=webhook_dev_mode,
        gitlab_webhook_secret=secret,
    )
    app = create_app(config, lifespan_override=_noop_lifespan)
    app.state.config = config
    app.state.issues_repo = object()
    app.state.issue_executor = object()
    app.state.integration_repo = _FakeIntegrationRepo()
    sync = _FakeGitLabSync() if wire else None
    app.state.gitlab_sync = sync
    return app, sync


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_token_is_compared_verbatim_not_as_an_hmac() -> None:
    """GitLab sends the secret itself — unlike GitHub's and Linear's HMACs."""
    app, sync = _app(secret="s3cret")
    body = json.dumps(PAYLOAD).encode()
    async with _client(app) as client:
        ok = await client.post("/api/v1/webhook/gitlab", content=body, headers={"X-Gitlab-Token": "s3cret"})
        bad = await client.post("/api/v1/webhook/gitlab", content=body, headers={"X-Gitlab-Token": "nope"})
    assert ok.status_code == 200, ok.text
    assert bad.status_code == 401
    assert len(sync.calls) == 1


async def test_missing_token_is_401_when_a_secret_is_configured() -> None:
    app, _ = _app(secret="s3cret")
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/gitlab", json=PAYLOAD)
    assert r.status_code == 401


async def test_unconfigured_secret_outside_dev_mode_is_503() -> None:
    app, _ = _app(secret="")
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/gitlab", json=PAYLOAD)
    assert r.status_code == 503


async def test_dev_mode_bypasses_only_an_empty_secret() -> None:
    """Same rule as GitHub and Linear — pinned because Linear's diverged once."""
    app, _ = _app(secret="", webhook_dev_mode=True)
    async with _client(app) as client:
        assert (await client.post("/api/v1/webhook/gitlab", json=PAYLOAD)).status_code == 200

    app, _ = _app(secret="s3cret", webhook_dev_mode=True)
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/gitlab", json=PAYLOAD, headers={"X-Gitlab-Token": "wrong"})
    assert r.status_code == 401


async def test_resolved_trigger_labels_are_passed_through() -> None:
    app, sync = _app(secret="", webhook_dev_mode=True)
    async with _client(app) as client:
        await client.post("/api/v1/webhook/gitlab", json=PAYLOAD)
    assert sync.trigger_labels == {"embry0"}


async def test_unconfigured_integration_reports_so() -> None:
    app, _ = _app(secret="", webhook_dev_mode=True, wire=False)
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/gitlab", json=PAYLOAD)
    assert r.status_code == 200
    assert r.json()["reason"] == "gitlab integration not configured"


async def test_smee_envelope_is_unwrapped() -> None:
    from tests.fixtures.webhooks import smee_envelope

    app, sync = _app(secret="", webhook_dev_mode=True)
    async with _client(app) as client:
        r = await client.post("/api/v1/webhook/gitlab", json=smee_envelope(PAYLOAD))
    assert r.status_code == 200
    assert sync.calls and sync.calls[0]["object_kind"] == "issue"


async def test_invalid_json_is_400() -> None:
    app, _ = _app(secret="", webhook_dev_mode=True)
    async with _client(app) as client:
        r = await client.post(
            "/api/v1/webhook/gitlab", content=b"not json", headers={"Content-Type": "application/json"}
        )
    assert r.status_code == 400
