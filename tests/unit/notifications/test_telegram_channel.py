"""TelegramChannel — native question messages with reply-to-answer wiring.

The channel sends each blocking question via the bot and persists the
returned Telegram message_id on the input row; the inbound webhook
(``/api/v1/telegram/callback``) matches replies against that id. Before this
channel existed, nothing ever wrote the mapping, so reply-matching was dead.
"""

from unittest.mock import AsyncMock, patch

import pytest

from embry0.notifications.channels import TelegramChannel
from embry0.notifications.dispatcher import dispatch_questions

_ISSUE = {"id": "iss-1", "title": "Fix the flux capacitor", "repo": "acme/widgets"}
_QUESTIONS = [
    {"input_id": "inp-1", "question": "Should we bump the major version?", "asking_node": "developer"},
]


def _channel(inputs_repo=None):
    return TelegramChannel(bot_token="t", chat_id="c", inputs_repo=inputs_repo or AsyncMock())


@pytest.mark.asyncio
async def test_dispatch_sends_and_persists_message_id():
    inputs_repo = AsyncMock()
    ch = _channel(inputs_repo)
    with patch("embry0.notifications.telegram.send_message", new=AsyncMock(return_value=42)) as send:
        await ch.dispatch(_ISSUE, _QUESTIONS)

    text = send.await_args.args[2]
    assert "Should we bump the major version?" in text
    assert "developer" in text
    assert "Reply to this message to answer." in text
    inputs_repo.set_telegram_message_id.assert_awaited_once_with("inp-1", 42)


@pytest.mark.asyncio
async def test_dispatch_leads_with_linear_identifier_when_present():
    """RAV-1076: chat references resolve naturally when the message names the ticket."""
    ch = _channel()
    issue = {**_ISSUE, "tracker": "linear", "tracker_key": "ENG-1234"}
    with patch("embry0.notifications.telegram.send_message", new=AsyncMock(return_value=42)) as send:
        await ch.dispatch(issue, _QUESTIONS)
    text = send.await_args.args[2]
    assert "ENG-1234 — Fix the flux capacitor" in text


@pytest.mark.asyncio
async def test_dispatch_send_failure_skips_mapping_write():
    inputs_repo = AsyncMock()
    ch = _channel(inputs_repo)
    with patch("embry0.notifications.telegram.send_message", new=AsyncMock(return_value=None)):
        await ch.dispatch(_ISSUE, _QUESTIONS)
    inputs_repo.set_telegram_message_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_mapping_write_failure_does_not_raise():
    inputs_repo = AsyncMock()
    inputs_repo.set_telegram_message_id.side_effect = RuntimeError("db down")
    ch = _channel(inputs_repo)
    with patch("embry0.notifications.telegram.send_message", new=AsyncMock(return_value=42)):
        await ch.dispatch(_ISSUE, _QUESTIONS)  # must not raise


@pytest.mark.asyncio
async def test_dispatcher_defaults_include_telegram_when_configured():
    ch = _channel()
    with patch.object(TelegramChannel, "dispatch", new=AsyncMock()) as d:
        await dispatch_questions(_QUESTIONS, dict(_ISSUE), telegram_channel=ch)
    d.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatcher_defaults_exclude_telegram_when_unconfigured():
    # No telegram channel injected and no explicit channels: only the
    # dashboard no-op runs; nothing raises about an unknown channel.
    await dispatch_questions(_QUESTIONS, dict(_ISSUE))


@pytest.mark.asyncio
async def test_dispatcher_respects_explicit_channel_optout():
    ch = _channel()
    issue = {**_ISSUE, "notification_channels": ["dashboard"]}
    with patch.object(TelegramChannel, "dispatch", new=AsyncMock()) as d:
        await dispatch_questions(_QUESTIONS, issue, telegram_channel=ch)
    d.assert_not_awaited()
