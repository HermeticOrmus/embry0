"""Per-principal rate limiter tests (RAV-1056 Phase C).

httpx ASGITransport lets each request carry a distinct client address, so
IP-vs-principal bucketing is observable. The middleware resolves creds via
the same cached resolver as the auth deps — the autouse buster mirrors
test_api_key_auth.py.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from embry0.api.deps import bust_api_key_cache, bust_user_gate_cache
from embry0.api.middleware.rate_limit import RateLimitMiddleware
from embry0.api.session import SESSION_COOKIE, SessionData, encode_session
from embry0.config import Embry0Config
from embry0.storage.repositories.api_keys import ApiKeyRow, hash_token

SECRET = "s" * 40


def _config() -> Embry0Config:
    with patch.dict(os.environ, {}, clear=True):
        return Embry0Config(
            _env_file=None,
            auth_dev_mode=False,
            api_key="master-key",
            auth0_issuer="https://t.example.com/",
            auth0_client_id="cid",
            auth0_client_secret="sec",
            session_secret=SECRET,
            sso_public_origin="https://e.example.com",
            # #393: SSO is on only with a roles claim, so a session-auth fixture states one.
            sso_roles_claim="https://example.com/roles",
        )


def _key_row(key_id: str, name: str) -> ApiKeyRow:
    return ApiKeyRow(
        id=key_id,
        user_id=None,
        name=name,
        prefix="emb_x",
        scope="operate",
        created_by="",
        created_at=None,
        expires_at=None,
        last_used_at=None,
        revoked_at=None,
    )


def _app(rpm: int = 2, webhook_rpm: int = 120) -> FastAPI:
    app = FastAPI()
    app.state.config = _config()
    keys_repo = MagicMock()
    tokens = {hash_token("emb_a"): _key_row("key-aaa", "a"), hash_token("emb_b"): _key_row("key-bbb", "b")}
    keys_repo.get_by_hash = AsyncMock(side_effect=lambda h: tokens.get(h))
    keys_repo.touch_last_used = AsyncMock()
    app.state.api_keys_repo = keys_repo
    app.add_middleware(RateLimitMiddleware, requests_per_minute=rpm, webhook_requests_per_minute=webhook_rpm)

    @app.get("/api/v1/ping")
    async def ping(request: Request) -> dict[str, bool]:
        return {"principal_state": hasattr(request.state, "principal")}

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/api/v1/webhook")
    async def webhook():
        return {"ok": True}

    @app.post("/api/v1/webhook/linear")
    async def webhook_linear():
        return {"ok": True}

    @app.post("/api/v1/telegram/callback")
    async def telegram_callback():
        return {"ok": True}

    return app


def _client(app: FastAPI, ip: str) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, client=(ip, 1234)), base_url="http://test")


@pytest.fixture(autouse=True)
def _caches():
    bust_api_key_cache()
    bust_user_gate_cache()
    yield
    bust_api_key_cache()
    bust_user_gate_cache()


@pytest.mark.asyncio
async def test_anonymous_buckets_are_per_ip():
    app = _app(rpm=2)
    async with _client(app, "10.0.0.1") as c1, _client(app, "10.0.0.2") as c2:
        assert (await c1.get("/api/v1/ping")).status_code == 200
        assert (await c1.get("/api/v1/ping")).status_code == 200
        third = await c1.get("/api/v1/ping")
        assert third.status_code == 429
        assert third.headers["Retry-After"] == "60"
        # a different IP is unaffected
        assert (await c2.get("/api/v1/ping")).status_code == 200


@pytest.mark.asyncio
async def test_same_key_from_two_ips_shares_bucket():
    app = _app(rpm=2)
    headers = {"Authorization": "Bearer emb_a"}
    async with _client(app, "10.0.0.1") as c1, _client(app, "10.0.0.2") as c2:
        assert (await c1.get("/api/v1/ping", headers=headers)).status_code == 200
        assert (await c2.get("/api/v1/ping", headers=headers)).status_code == 200
        assert (await c1.get("/api/v1/ping", headers=headers)).status_code == 429


@pytest.mark.asyncio
async def test_two_keys_have_independent_buckets():
    app = _app(rpm=2)
    async with _client(app, "10.0.0.1") as c:
        for _ in range(2):
            assert (await c.get("/api/v1/ping", headers={"Authorization": "Bearer emb_a"})).status_code == 200
        assert (await c.get("/api/v1/ping", headers={"Authorization": "Bearer emb_a"})).status_code == 429
        assert (await c.get("/api/v1/ping", headers={"Authorization": "Bearer emb_b"})).status_code == 200


@pytest.mark.asyncio
async def test_master_key_bucket_distinct_from_ip():
    app = _app(rpm=2)
    async with _client(app, "10.0.0.1") as c:
        for _ in range(2):
            assert (await c.get("/api/v1/ping")).status_code == 200
        assert (await c.get("/api/v1/ping")).status_code == 429
        # same IP, master credential: separate bucket, still admitted
        assert (await c.get("/api/v1/ping", headers={"Authorization": "Bearer master-key"})).status_code == 200


@pytest.mark.asyncio
async def test_invalid_bearer_shares_ip_bucket():
    app = _app(rpm=2)
    async with _client(app, "10.0.0.1") as c:
        assert (await c.get("/api/v1/ping", headers={"Authorization": "Bearer garbage"})).status_code == 200
        assert (await c.get("/api/v1/ping")).status_code == 200
        # anonymous and invalid-token traffic drained the same bucket
        assert (await c.get("/api/v1/ping", headers={"Authorization": "Bearer garbage2"})).status_code == 429


@pytest.mark.asyncio
async def test_session_cookie_buckets_by_email():
    app = _app(rpm=2)
    cookie = encode_session(
        SECRET,
        SessionData(sub="auth0|op", email="Op@example.com", name="O", roles=[], tier="operator", epoch=1),
    )
    async with _client(app, "10.0.0.1") as c1, _client(app, "10.0.0.2") as c2:
        c1.cookies.set(SESSION_COOKIE, cookie)
        c2.cookies.set(SESSION_COOKIE, cookie)
        assert (await c1.get("/api/v1/ping")).status_code == 200
        assert (await c2.get("/api/v1/ping")).status_code == 200
        # same user from a third address: bucket exhausted
        async with _client(app, "10.0.0.3") as c3:
            c3.cookies.set(SESSION_COOKIE, cookie)
            assert (await c3.get("/api/v1/ping")).status_code == 429


@pytest.mark.asyncio
async def test_resolver_crash_falls_back_to_ip(monkeypatch):
    import embry0.api.deps as deps_mod

    async def boom(app, token):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(deps_mod, "resolve_bearer_principal", boom)
    app = _app(rpm=2)
    async with _client(app, "10.0.0.1") as c:
        resp = await c.get("/api/v1/ping", headers={"Authorization": "Bearer emb_a"})
        assert resp.status_code == 200  # no 500 — fell back to the IP bucket


@pytest.mark.asyncio
async def test_exempt_paths_never_limited():
    app = _app(rpm=1)
    async with _client(app, "10.0.0.1") as c:
        for _ in range(3):
            assert (await c.get("/health")).status_code == 200


# ---- RAV-1070: webhook endpoints get an IP-keyed throttle, not an exemption ----


@pytest.mark.asyncio
async def test_webhook_throttled_per_ip():
    app = _app(rpm=100, webhook_rpm=2)
    async with _client(app, "10.0.0.1") as c1, _client(app, "10.0.0.2") as c2:
        assert (await c1.post("/api/v1/webhook")).status_code == 200
        assert (await c1.post("/api/v1/webhook/linear")).status_code == 200
        third = await c1.post("/api/v1/webhook")
        assert third.status_code == 429
        assert third.headers["Retry-After"] == "60"
        # a different source IP is unaffected
        assert (await c2.post("/api/v1/webhook")).status_code == 200


@pytest.mark.asyncio
async def test_webhook_bucket_disjoint_from_api_ip_bucket():
    """Exhausting the webhook window must not consume the same IP's API budget."""
    app = _app(rpm=2, webhook_rpm=1)
    async with _client(app, "10.0.0.1") as c:
        assert (await c.post("/api/v1/webhook")).status_code == 200
        assert (await c.post("/api/v1/webhook")).status_code == 429
        assert (await c.get("/api/v1/ping")).status_code == 200


@pytest.mark.asyncio
async def test_webhook_ceiling_independent_of_api_ceiling():
    """A tight API rpm must not choke webhook deliveries (separate ceiling)."""
    app = _app(rpm=1, webhook_rpm=3)
    async with _client(app, "10.0.0.1") as c:
        for _ in range(3):
            assert (await c.post("/api/v1/webhook")).status_code == 200
        assert (await c.post("/api/v1/webhook")).status_code == 429


# ---- #247: the Telegram callback lost its exemption, under its own bucket ----


@pytest.mark.asyncio
async def test_telegram_callback_throttled_per_ip():
    app = _app(rpm=100, webhook_rpm=2)
    async with _client(app, "10.0.0.1") as c1, _client(app, "10.0.0.2") as c2:
        for _ in range(2):
            assert (await c1.post("/api/v1/telegram/callback")).status_code == 200
        assert (await c1.post("/api/v1/telegram/callback")).status_code == 429
        assert (await c2.post("/api/v1/telegram/callback")).status_code == 200


@pytest.mark.asyncio
async def test_telegram_bucket_disjoint_from_webhook_bucket():
    """A GitHub delivery storm must not starve the bot, nor the reverse."""
    app = _app(rpm=100, webhook_rpm=1)
    async with _client(app, "10.0.0.1") as c:
        assert (await c.post("/api/v1/webhook")).status_code == 200
        assert (await c.post("/api/v1/webhook")).status_code == 429
        assert (await c.post("/api/v1/telegram/callback")).status_code == 200


@pytest.mark.asyncio
async def test_middleware_does_not_set_principal_state():
    app = _app(rpm=5)
    async with _client(app, "10.0.0.1") as c:
        body = (await c.get("/api/v1/ping", headers={"Authorization": "Bearer emb_a"})).json()
    assert body["principal_state"] is False
