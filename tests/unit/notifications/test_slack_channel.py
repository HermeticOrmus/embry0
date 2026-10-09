"""SlackChannel — outbound agent questions via a Slack incoming webhook.

The URL is read from IntegrationConfigRepository at send time (the store the
settings page writes). An empty URL skips the POST. Slack is send-only: the
text carries the same facts as TelegramChannel and does not ask for a reply.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from embry0.notifications.dispatcher import dispatch_questions
from embry0.storage.repositories.integration_config import IntegrationConfigRepository

_ISSUE = {"id": "iss-1", "title": "Fix the flux capacitor", "repo": "acme/widgets"}
_QUESTIONS = [
    {
        "input_id": "inp-1",
        "question": "Should we bump the major version?",
        "asking_node": "developer",
    },
]
# Not a real Slack host. MockTransport never opens a socket; a client that
# ignores the injection fails fast on a closed port instead of calling out.
_URL = "http://127.0.0.1:9/services/T/B/secret"


def _repo(url: str) -> MagicMock:
    repo = MagicMock()
    repo.get_slack_webhook_url = AsyncMock(return_value=url)
    return repo


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _slack_channel(repo, client):
    from embry0.notifications.channels import SlackChannel

    return SlackChannel(repo, http_client=client)


@pytest.mark.asyncio
async def test_configured_url_posts_question_text():
    """A stored URL receives one POST whose JSON text contains the question."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    repo = _repo(_URL)
    channel = _slack_channel(repo, _client(handler))
    repo.get_slack_webhook_url.assert_not_awaited()

    await channel.dispatch(dict(_ISSUE), list(_QUESTIONS))

    assert len(seen) == 1, "configured slack webhook must receive exactly one POST"
    repo.get_slack_webhook_url.assert_awaited()
    body = json.loads(seen[0].content)
    assert set(body) == {"text"}, "slack incoming webhook body must be exactly {text}"
    text = body["text"]
    assert "Should we bump the major version?" in text, "slack text must contain the question"
    assert "developer" in text, "slack text must name the asking node"
    assert "Fix the flux capacitor" in text, "slack text must name the issue"
    assert "reply to this message" not in text.lower(), "slack is outbound only; do not ask for a reply"


@pytest.mark.asyncio
async def test_empty_url_performs_no_post():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    channel = _slack_channel(_repo(""), _client(handler))
    await channel.dispatch(dict(_ISSUE), list(_QUESTIONS))
    assert seen == [], "empty slack webhook URL must not POST"


@pytest.mark.asyncio
async def test_text_leads_with_linear_identifier_when_present():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    issue = {**_ISSUE, "tracker": "linear", "tracker_key": "ENG-1234"}
    channel = _slack_channel(_repo(_URL), _client(handler))
    await channel.dispatch(issue, list(_QUESTIONS))
    text = json.loads(seen[0].content)["text"]
    assert "ENG-1234 — Fix the flux capacitor" in text, "slack text must lead with the Linear identifier"


@pytest.mark.asyncio
async def test_http_failure_is_swallowed_and_url_is_not_logged():
    """A non-2xx is logged by status and input id only, and does not raise."""
    import structlog

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"nope {_URL}")

    channel = _slack_channel(_repo(_URL), _client(handler))
    with structlog.testing.capture_logs() as logs:
        await channel.dispatch(dict(_ISSUE), list(_QUESTIONS))

    rendered = repr(logs)
    assert _URL not in rendered, "slack failure log must not contain the webhook URL"
    assert "secret" not in rendered, "slack failure log must not echo the webhook URL"
    assert any(
        entry.get("event") == "slack_send_failed" and entry.get("status") == 500 and entry.get("input_id") == "inp-1"
        for entry in logs
    ), "slack failure log must carry status code and input id"


@pytest.mark.asyncio
async def test_connect_error_is_swallowed_and_url_is_not_logged():
    import structlog

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection failed for {_URL}")

    channel = _slack_channel(_repo(_URL), _client(handler))
    with structlog.testing.capture_logs() as logs:
        await channel.dispatch(dict(_ISSUE), list(_QUESTIONS))

    rendered = repr(logs)
    assert _URL not in rendered, "slack failure log must not contain the webhook URL"
    assert any(entry.get("input_id") == "inp-1" for entry in logs), "slack failure log must carry the input id"


@pytest.mark.asyncio
async def test_dispatcher_defaults_include_slack_when_injected():
    channel = MagicMock()
    channel.dispatch = AsyncMock()
    await dispatch_questions(list(_QUESTIONS), dict(_ISSUE), slack_channel=channel)
    channel.dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatcher_explicit_channels_omit_slack_unless_listed():
    channel = MagicMock()
    channel.dispatch = AsyncMock()
    issue = {**_ISSUE, "notification_channels": ["dashboard"]}
    await dispatch_questions(list(_QUESTIONS), issue, slack_channel=channel)
    channel.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_slack_webhook_url_returns_the_stored_value():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={"slack_webhook_url": "https://hooks.slack.com/services/T/B/raw"})
    repo = IntegrationConfigRepository(db)
    assert await repo.get_slack_webhook_url() == "https://hooks.slack.com/services/T/B/raw"


@pytest.mark.asyncio
async def test_get_slack_webhook_url_prefers_db_over_env(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/ENV")
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={"slack_webhook_url": "https://hooks.slack.com/services/DB"})
    repo = IntegrationConfigRepository(db)
    assert await repo.get_slack_webhook_url() == "https://hooks.slack.com/services/DB"


@pytest.mark.asyncio
async def test_get_slack_webhook_url_falls_back_to_env_when_empty(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/ENV")
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={"slack_webhook_url": ""})
    repo = IntegrationConfigRepository(db)
    assert await repo.get_slack_webhook_url() == "https://hooks.slack.com/services/ENV"


@pytest.mark.asyncio
async def test_get_slack_webhook_url_empty_when_unset(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=None)
    repo = IntegrationConfigRepository(db)
    assert await repo.get_slack_webhook_url() == ""
