from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config

_GET_RESPONSE = {
    "trigger_labels": ["embry0"],
    "webhook_secret_set": False,
    "slack_webhook_url_set": False,
    "slack_webhook_url_masked": "",
    "telegram_bot_token_set": False,
    "telegram_bot_token_masked": "",
    "telegram_chat_id": "",
}

_UPDATE_RESPONSE = {
    "trigger_labels": ["embry0", "AutoFix"],
    "webhook_secret_set": False,
    "slack_webhook_url_set": False,
    "slack_webhook_url_masked": "",
    "telegram_bot_token_set": False,
    "telegram_bot_token_masked": "",
    "telegram_chat_id": "",
}


@pytest.fixture
def app():
    config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True)
    app = create_app(config)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_GET_RESPONSE)
    repo.update = AsyncMock(return_value=_UPDATE_RESPONSE)
    app.state.integration_repo = repo
    return app


@pytest.mark.asyncio
async def test_get_integrations(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/config/integrations")
    assert resp.status_code == 200
    assert resp.json()["trigger_labels"] == ["embry0"]


@pytest.mark.asyncio
async def test_update_integrations(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/config/integrations",
            json={"trigger_labels": ["embry0", "AutoFix"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 200
    assert resp.json()["trigger_labels"] == ["embry0", "AutoFix"]


# ---------------------------------------------------------------------------
# Linear fields + audit redaction (#157)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_linear_fields_reach_the_repository(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/config/integrations",
            json={
                "linear_api_key": "lin_api_secret",
                "linear_webhook_secret": "whsec_1",
                "linear_repo_map": '{"P": "o/r"}',
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 200
    kwargs = app.state.integration_repo.update.await_args.kwargs
    assert kwargs["linear_api_key"] == "lin_api_secret"
    assert kwargs["linear_webhook_secret"] == "whsec_1"
    assert kwargs["linear_repo_map"] == '{"P": "o/r"}'


@pytest.mark.asyncio
async def test_audit_never_records_credential_values(app, monkeypatch):
    """The audit row logged secrets verbatim before #157."""
    recorded: dict = {}

    async def _capture(db, event, **kwargs):
        recorded.update(kwargs.get("details") or {})

    monkeypatch.setattr("embry0.api.v1.config.emit_audit", _capture)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/api/v1/config/integrations",
            json={
                "linear_api_key": "lin_api_secret",
                "webhook_secret": "gh_secret",
                "telegram_chat_id": "12345",
                "trigger_labels": ["embry0"],
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    assert "lin_api_secret" not in str(recorded)
    assert "gh_secret" not in str(recorded)
    assert recorded["linear_api_key"] == "<set>"
    assert recorded["webhook_secret"] == "<set>"
    # Non-secret fields stay legible — the audit trail is still useful.
    assert recorded["telegram_chat_id"] == "12345"
    assert recorded["trigger_labels"] == ["embry0"]
