"""Slack incoming-webhook sender for agent questions.

Outbound only. The caller reads the URL at send time; this module only POSTs
``{"text": ...}``. Failures are logged by status code and input id — never the
URL or the response body, both of which can echo the webhook secret.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


async def post_incoming_webhook(
    url: str,
    text: str,
    *,
    input_id: Any,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """POST one question to a Slack incoming webhook. Never raises."""
    client = http_client or httpx.AsyncClient(timeout=15.0)
    try:
        try:
            response = await client.post(url, json={"text": text})
        except Exception:
            logger.warning("slack_send_failed", input_id=input_id)
            return
        if response.status_code >= 300:
            logger.warning("slack_send_failed", status=response.status_code, input_id=input_id)
    finally:
        if http_client is None:
            await client.aclose()
