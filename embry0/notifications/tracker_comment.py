"""Tracker comment channel — write-back to an issue's originating tracker (#163).

One channel class serves every tracker: it owns the three message formats
(questions asked / answer recorded / job outcome) and delegates transport to
the tracker's ``post_issue_comment`` (#160). Before this module, Linear
write-back was hard-wired via ``getattr(self, "_linear_sync", None)`` special
cases and GitLab had none at all — ``GitLabSyncService.post_outcome`` existed,
unit-tested, with no caller.

The channel resolves its tracker through the :class:`TrackerRegistry` at
call time, not at construction: the Linear webhook route re-registers a
fresh instance per delivery when DB-managed credentials change, and a
channel pinned to the startup instance would keep posting with stale keys.

``dispatch`` conforms to :class:`embry0.notifications.channels.NotificationChannel`,
so a tracker channel can also be selected explicitly via an issue's
``notification_channels``. ``post_answer`` / ``post_outcome`` are the
tracker-trail surface the executor and answer recorder call unconditionally
for the issue's originating tracker.
"""

from __future__ import annotations

from typing import Any

import structlog

from embry0.notifications.public_errors import public_error_message
from embry0.safety.error_codes import ErrorCode
from embry0.trackers import TrackerIssueRef, TrackerRegistry

logger = structlog.get_logger(__name__)

# Kept short so a long agent question doesn't dominate the ticket thread —
# the full text is always one click away on the dashboard input row.
_QUESTION_EXCERPT_CHARS = 500
_ANSWER_EXCERPT_CHARS = 2000


def _excerpt(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


class TrackerCommentChannel:
    """Comments embry0's decision trail onto one tracker's issues."""

    def __init__(self, registry: TrackerRegistry, name: str) -> None:
        self._registry = registry
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def _ref(self, issue: dict[str, Any] | None) -> TrackerIssueRef | None:
        """The comment address, or None when this issue is not ours."""
        if not issue or issue.get("tracker") != self._name:
            return None
        key = str(issue.get("tracker_key") or "")
        native_id = str(issue.get("tracker_native_id") or "")
        if not key and not native_id:
            return None
        return TrackerIssueRef(repo=issue.get("repo") or "", key=key, native_id=native_id)

    async def _post(self, issue: dict[str, Any] | None, body: str) -> bool:
        ref = self._ref(issue)
        if ref is None:
            return False
        tracker = self._registry.get(self._name)
        if tracker is None:
            logger.warning("tracker_channel_unwired", tracker=self._name, issue_id=(issue or {}).get("id"))
            return False
        return await tracker.post_issue_comment(ref, body)

    async def dispatch(self, issue: dict[str, Any], questions: list[dict[str, Any]]) -> None:
        """RAV-1076: the decision trail starts on the ticket — post the
        blocking agent questions as one comment. Answers follow via
        :meth:`post_answer` from whichever channel they land."""
        if not questions:
            return
        job_id = next((q.get("job_id") for q in questions if q.get("job_id")), "?")
        asking_node = next((q.get("asking_node") for q in questions if q.get("asking_node")), "agent")
        lines = [f"**embry0 needs input** — `{job_id}`, asked by {asking_node}"]
        for q in questions:
            lines.append(_quote(str(q.get("question") or "")))
        lines.append("")
        lines.append(
            "_Answer via the dashboard, by replying to the Telegram question "
            "message, or through the embry0 MCP `job_answer` tool — answers "
            "are recorded here._"
        )
        await self._post(issue, "\n".join(lines))

    async def post_answer(
        self,
        issue: dict[str, Any] | None,
        inp: dict[str, Any],
        answer: str,
        *,
        answered_by: str,
        channel: str,
    ) -> None:
        """Comment the Q&A pair on the linked tracker issue. Best-effort."""
        question = _excerpt(str(inp.get("question") or ""), _QUESTION_EXCERPT_CHARS)
        lines = [
            f"**Agent question answered** — `{inp.get('job_id', '?')}`, asked by {inp.get('asking_node') or 'agent'}",
            _quote(question),
            "",
            f"**Answer** (by {answered_by} via {channel}):",
            "",
            _excerpt(answer, _ANSWER_EXCERPT_CHARS),
        ]
        await self._post(issue, "\n".join(lines))

    async def post_outcome(self, issue: dict[str, Any] | None, job: dict[str, Any]) -> None:
        """Comment the terminal job outcome on the source tracker issue."""
        status = job.get("status") or "unknown"
        lines = [f"**embry0 job `{job.get('job_id', '?')}` finished: {status}**"]
        if job.get("pr_url"):
            change_request = "Merge request" if self._name == "gitlab" else "Pull request"
            lines.append(f"{change_request}: {job['pr_url']}")
        elif job.get("result_summary"):
            # RAV-601: non-code jobs deliver text, not a PR — surface the
            # finalize_output summary as the outcome.
            lines.append(f"Result:\n\n{str(job['result_summary'])[:4000]}")
        if job.get("error_message") or job.get("error_code"):
            # Never the raw message: it is str(exc) on most paths (paths,
            # hostnames, now and then a token or a figure) and the ticket may
            # be readable company-wide. Code + a fixed sentence; the detail
            # stays on the job's dashboard page. Spend is not posted either.
            code = job.get("error_code") or ErrorCode.UNKNOWN.value
            lines.append(
                f"Error: `{code}` — {public_error_message(code)}. "
                f"Details are on the page of job `{job.get('job_id', '?')}` in the embry0 dashboard."
            )
        await self._post(issue, "\n".join(lines))
