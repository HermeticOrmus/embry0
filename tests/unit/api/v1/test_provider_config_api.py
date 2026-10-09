from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config

_GET_RESPONSE = {
    "model_heavy": "claude-opus-4-6",
    "model_medium": "claude-sonnet-4-6",
    "model_light": "claude-haiku-4-5",
    "default_model": "",
    "api_key_set": False,
    "oauth_token_set": False,
}

_UPDATE_RESPONSE = {
    **_GET_RESPONSE,
    "model_heavy": "claude-opus-4-7",
}


@pytest.fixture
def app():
    config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True)
    app = create_app(config)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_GET_RESPONSE)
    repo.update = AsyncMock(return_value=_UPDATE_RESPONSE)
    repo.test_connection = AsyncMock(return_value={"status": "ok", "message": "Anthropic API key is set."})
    app.state.provider_repo = repo
    return app


@pytest.mark.asyncio
async def test_get_provider(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/config/provider")
    assert resp.status_code == 200
    assert resp.json()["model_heavy"] == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_update_provider(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/config/provider",
            json={"model_heavy": "claude-opus-4-7"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 200
    assert resp.json()["model_heavy"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_provider_test_connection(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/config/provider/test",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_provider_test_emits_status_only_audit(app, monkeypatch):
    from unittest.mock import AsyncMock as _AsyncMock

    emit = _AsyncMock()
    monkeypatch.setattr("embry0.api.v1.config.emit_audit", emit)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/v1/config/provider/test", headers={"X-Requested-With": "XMLHttpRequest"})
    assert emit.call_args.args[1] == "provider_config_tested"
    assert emit.call_args.kwargs["details"] == {"status": "ok"}


# --- RAV-1056 Phase C: PUT /config/provider is admin-gated (like integrations) ---


def test_update_provider_requires_admin_outside_dev_mode():
    """Operator session -> 403; admin session -> 200. Stub-app style with the
    real auth chain (dev-mode create_app tests above bypass the gate)."""
    import os
    from unittest.mock import patch as _patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from embry0.api.deps import bust_user_gate_cache, require_auth
    from embry0.api.session import SESSION_COOKIE, SessionData, encode_session
    from embry0.api.v1 import config as config_v1

    secret = "s" * 40
    with _patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(
            _env_file=None,
            auth_dev_mode=False,
            api_key="master-key",
            auth0_issuer="https://t.example.com/",
            auth0_client_id="cid",
            auth0_client_secret="sec",
            session_secret=secret,
            sso_public_origin="https://e.example.com",
            # #393: SSO is on only with a roles claim, so a session-auth fixture states one.
            sso_roles_claim="https://example.com/roles",
        )
    app = FastAPI()
    app.state.config = cfg
    app.state.db = None
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_GET_RESPONSE)
    repo.update = AsyncMock(return_value=_UPDATE_RESPONSE)
    app.state.provider_repo = repo
    user = MagicMock()
    user.session_epoch = 1
    user.disabled = False
    users_repo = MagicMock()
    users_repo.get_by_sub = AsyncMock(return_value=user)
    app.state.users_repo = users_repo
    app.include_router(config_v1.router, prefix="/api/v1", dependencies=[require_auth])

    def cookie(tier: str) -> str:
        return encode_session(
            secret,
            SessionData(sub="auth0|u", email="u@example.com", name="U", roles=[], tier=tier, epoch=1),
        )

    bust_user_gate_cache()
    try:
        client = TestClient(app)
        client.cookies.set(SESSION_COOKIE, cookie("operator"))
        resp = client.put("/api/v1/config/provider", json={"model_heavy": "claude-opus-5"})
        assert resp.status_code == 403

        bust_user_gate_cache()
        client = TestClient(app)
        client.cookies.set(SESSION_COOKIE, cookie("admin"))
        resp = client.put("/api/v1/config/provider", json={"model_heavy": "claude-opus-5"})
        assert resp.status_code == 200
    finally:
        bust_user_gate_cache()


# --- RAV-1297: disabled_models interlock on PUT /config/provider ---


@pytest.mark.asyncio
async def test_update_provider_rejects_disabling_tier_model(app):
    # _GET_RESPONSE has model_light=claude-haiku-4-5 — disabling it would
    # break the tier fallback itself.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/config/provider",
            json={"disabled_models": ["claude-haiku-4-5"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 422
    assert "model_light=claude-haiku-4-5" in resp.json()["detail"]
    app.state.provider_repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_provider_rejects_unknown_disabled_model(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/config/provider",
            json={"disabled_models": ["gpt-4"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 422
    assert "unknown model ids" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_provider_rejects_tier_set_to_disabled_model(app):
    app.state.provider_repo.get = AsyncMock(return_value={**_GET_RESPONSE, "disabled_models": ["claude-sonnet-5"]})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/config/provider",
            json={"model_medium": "claude-sonnet-5"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_provider_accepts_valid_disable(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/config/provider",
            json={"disabled_models": ["grok-4.5"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 200
    app.state.provider_repo.update.assert_awaited_once_with(disabled_models=["grok-4.5"])


# --- #465: default_model is validated on PUT /config/provider ---
# The conflict tests use grok-4.5, a catalogue model before and after #465,
# so the 422 they pin is the interlock's and not "unknown model id".


async def _put_provider(app, body: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.put(
            "/api/v1/config/provider",
            json=body,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )


@pytest.mark.asyncio
async def test_update_provider_accepts_known_default_model(app):
    resp = await _put_provider(app, {"default_model": "grok-4.7"})

    assert resp.status_code == 200, resp.text
    app.state.provider_repo.update.assert_awaited_once_with(default_model="grok-4.7")


@pytest.mark.asyncio
async def test_update_provider_rejects_unknown_default_model(app):
    resp = await _put_provider(app, {"default_model": "grok-9-imaginary"})

    assert resp.status_code == 422, f"an uncatalogued default_model must be refused, got {resp.status_code}"
    assert "default_model is not a known model id" in resp.json()["detail"]
    assert "grok-9-imaginary" in resp.json()["detail"]
    app.state.provider_repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_provider_rejects_default_model_that_is_disabled(app):
    app.state.provider_repo.get = AsyncMock(return_value={**_GET_RESPONSE, "disabled_models": ["grok-4.5"]})

    resp = await _put_provider(app, {"default_model": "grok-4.5"})

    assert resp.status_code == 422, f"a disabled default_model must be refused, got {resp.status_code}"
    assert "default_model=grok-4.5" in resp.json()["detail"]
    app.state.provider_repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_provider_rejects_disabling_the_default_model(app):
    """The same conflict from the other side: the default is already saved."""
    app.state.provider_repo.get = AsyncMock(return_value={**_GET_RESPONSE, "default_model": "grok-4.5"})

    resp = await _put_provider(app, {"disabled_models": ["grok-4.5"]})

    assert resp.status_code == 422, f"disabling the default model must be refused, got {resp.status_code}"
    assert "default_model=grok-4.5" in resp.json()["detail"]
    app.state.provider_repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_provider_rejects_default_and_disable_in_one_request(app):
    resp = await _put_provider(app, {"default_model": "grok-4.5", "disabled_models": ["grok-4.5"]})

    assert resp.status_code == 422, f"one request must not both set and disable a model, got {resp.status_code}"
    assert "default_model=grok-4.5" in resp.json()["detail"]
    app.state.provider_repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_provider_can_clear_the_default_model(app):
    app.state.provider_repo.get = AsyncMock(return_value={**_GET_RESPONSE, "default_model": "grok-4.7"})

    resp = await _put_provider(app, {"default_model": ""})

    assert resp.status_code == 200, resp.text
    app.state.provider_repo.update.assert_awaited_once_with(default_model="")


@pytest.mark.asyncio
async def test_update_provider_tier_edit_is_not_blocked_by_a_stale_stored_default(app):
    """A default saved before it was validated must not lock the rest of the form."""
    app.state.provider_repo.get = AsyncMock(return_value={**_GET_RESPONSE, "default_model": "retired-model"})

    resp = await _put_provider(app, {"model_heavy": "claude-opus-5"})

    assert resp.status_code == 200, resp.text
