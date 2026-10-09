"""register_webhook only subscribes to the update shapes the callback handles (#247)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_register_webhook_restricts_allowed_updates():
    from embry0.notifications.telegram import register_webhook

    resp = MagicMock(status_code=200, text="{}")
    resp.json.return_value = {"ok": True}
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("embry0.notifications.telegram.httpx.AsyncClient", return_value=client):
        ok = await register_webhook("tok", "https://x/api/v1/telegram/callback", "s3cret")

    assert ok is True
    _, kwargs = client.post.call_args
    assert kwargs["json"]["allowed_updates"] == ["message", "callback_query"]
    assert kwargs["json"]["secret_token"] == "s3cret"
