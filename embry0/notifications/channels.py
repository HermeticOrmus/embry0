"""Notification channel abstraction.

A channel knows how to dispatch one batch of agent questions to one
specific surface (dashboard, Telegram chat, Slack incoming webhook,
GitHub issue comment). The fan-out helper invokes every requested
channel concurrently with isolated try/except so one channel's failure
can't block another.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import structlog

from embry0.trackers import linear_identifier_of

logger = structlog.get_logger(__name__)


class NotificationChannel(Protocol):
    """Each channel has one async dispatch method.

    Args:
      issue: dict with at least {id, repo, tracker, tracker_key, title}.
      questions: list of dicts, each with {question, input_id, options, asking_node}.
    """

    async def dispatch(self, issue: dict[str, Any], questions: list[dict[str, Any]]) -> None: ...


class DashboardChannel:
    """No-op channel — dashboard reads from issue_inputs directly. Included
    for symmetry so dispatch_to_channels can iterate uniformly."""

    async def dispatch(self, issue: dict[str, Any], questions: list[dict[str, Any]]) -> None:
        logger.debug("dashboard_channel_noop", issue_id=issue.get("id"), n=len(questions))


async def dispatch_to_channels(
    channels: list[str],
    registry: dict[str, NotificationChannel],
    issue: dict[str, Any],
    questions: list[dict[str, Any]],
) -> None:
    """Dispatch to every channel in `channels`. Unknown channels are skipped
    with a warning. Per-channel failures are logged and swallowed."""

    async def _safe_dispatch(name: str) -> None:
        ch = registry.get(name)
        if ch is None:
            logger.warning(
                "unknown_notification_channel",
                channel=name,
                issue_id=issue.get("id"),
            )
            return
        try:
            await ch.dispatch(issue, questions)
        except Exception as exc:
            logger.warning(
                "channel_dispatch_failed",
                channel=name,
                issue_id=issue.get("id"),
                error=str(exc),
            )

    await asyncio.gather(*(_safe_dispatch(c) for c in channels), return_exceptions=False)


class TelegramChannel:
    """Sends each blocking agent question as its own Telegram message and
    persists the returned message_id on the pending input.

    The persisted id is the reply-to-answer link (RAV-1060 follow-up): the
    inbound webhook (``/api/v1/telegram/callback``) matches a human's
    reply-to-message to the input via ``get_by_telegram_message`` and
    resumes the pipeline. Before this channel existed embry0 never sent
    question messages itself (only the external webhook relay did), so the
    mapping table stayed empty and replies could never match.
    """

    def __init__(self, *, bot_token: str, chat_id: str, inputs_repo: Any) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._inputs = inputs_repo

    async def dispatch(self, issue: dict[str, Any], questions: list[dict[str, Any]]) -> None:
        from embry0.notifications.telegram import send_message

        for q in questions:
            header = f"embry0 needs input — {q.get('asking_node') or 'agent'}"
            context = issue.get("title") or issue.get("repo") or issue.get("id") or ""
            # RAV-1076: lead with the Linear identifier so chat references
            # ("answer the ENG-123 question") resolve without a lookup.
            linear_identifier = linear_identifier_of(issue)
            if linear_identifier:
                context = f"{linear_identifier} — {context}" if context else str(linear_identifier)
            text = f"{header}\n{context}\n\n{q['question']}\n\nReply to this message to answer."
            message_id = await send_message(self._bot_token, self._chat_id, text)
            if message_id is None:
                logger.warning("telegram_channel_send_failed", input_id=q.get("input_id"))
                continue
            try:
                await self._inputs.set_telegram_message_id(q["input_id"], message_id)
            except Exception:
                # The question was delivered; a failed mapping write only
                # degrades reply-matching for this one input.
                logger.warning(
                    "telegram_channel_mapping_write_failed",
                    input_id=q.get("input_id"),
                    message_id=message_id,
                    exc_info=True,
                )


class SlackChannel:
    """Posts each blocking agent question to the configured Slack incoming webhook.

    Outbound only — there is no reply path, so the text does not ask the
    reader to reply. The URL is read on every send from
    ``IntegrationConfigRepository`` (the store the settings page writes), so a
    save applies without a process restart. An empty URL is a debug skip.
    HTTP failures are logged with the status code and input id only, and
    swallowed so the rest of the fan-out still runs.
    """

    def __init__(self, integration_repo: Any, *, http_client: Any | None = None) -> None:
        self._integrations = integration_repo
        self._http_client = http_client

    async def dispatch(self, issue: dict[str, Any], questions: list[dict[str, Any]]) -> None:
        from embry0.notifications.slack import post_incoming_webhook

        for q in questions:
            input_id = q.get("input_id")
            try:
                url = (await self._integrations.get_slack_webhook_url() or "").strip()
            except Exception:
                # No webhook URL in a config-read error, so the traceback is safe.
                # HTTP failures are logged without one: httpx puts the URL in the message.
                logger.warning("slack_config_read_failed", input_id=input_id, exc_info=True)
                continue
            if not url:
                logger.debug("slack_skipped_unconfigured", input_id=input_id)
                continue
            header = f"embry0 needs input — {q.get('asking_node') or 'agent'}"
            context = issue.get("title") or issue.get("repo") or issue.get("id") or ""
            linear_identifier = linear_identifier_of(issue)
            if linear_identifier:
                context = f"{linear_identifier} — {context}" if context else str(linear_identifier)
            text = f"{header}\n{context}\n\n{q['question']}"
            await post_incoming_webhook(url, text, input_id=input_id, http_client=self._http_client)
