"""Provider rate-limit and auth signals on SdkAgentExecutor.

SDK <0.2 has no api_error_status. Every signal is read with getattr, and a
message that lacks the field stays a success.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from embry0.agents.executor import SdkAgentExecutor
from embry0.agents.invocation import AgentInvocation
from embry0.safety.policy import default_policy_for_agent

_RATE = "ERR_PROVIDER_RATE_LIMITED"
_QUOTA = "ERR_PROVIDER_QUOTA_EXHAUSTED"
_AUTH = "ERR_AUTH_REJECTED"


def _inv() -> AgentInvocation:
    return AgentInvocation(
        agent_type="developer",
        prompt="say hello",
        system_prompt="",
        system_context="",
        model="claude-sonnet-4-6",
        tools=["Read"],
        skills=[],
        mcp_servers={},
        max_turns=5,
        timeout_seconds=60,
        execution_mode="sdk",
        auth_mode="api_key",
        safety_policy=default_policy_for_agent("developer"),
        channel_config=None,
    )


class _OldResult:
    """SDK <0.2 shape: cost and result, no is_error and no api_error_status."""

    def __init__(self, result: str) -> None:
        self.result = result
        self.total_cost_usd = 0.0
        self.num_turns = 1
        self.duration_ms = 1
        self.usage = {}


class _Result:
    def __init__(
        self,
        *,
        is_error: bool = False,
        api_error_status: int | None = None,
        errors: list[str] | None = None,
        result: str = "",
        subtype: str = "success",
    ) -> None:
        self.is_error = is_error
        self.api_error_status = api_error_status
        self.errors = errors
        self.result = result
        self.subtype = subtype
        self.total_cost_usd = 0.0
        self.num_turns = 1
        self.duration_ms = 1
        self.usage = {}


class _Assistant:
    def __init__(self, error: str | None) -> None:
        self.content = []
        self.model = "claude-sonnet-4-6"
        self.error = error


class _RateInfo:
    def __init__(self, status: str) -> None:
        self.status = status


class _RateEvent:
    def __init__(self, status: str) -> None:
        self.rate_limit_info = _RateInfo(status)
        self.uuid = "u"
        self.session_id = "s"


async def _scripted(messages: list[Any]):
    for message in messages:
        yield message


async def _run(messages: list[Any], tmp_path) -> Any:
    import os

    os.environ["EMBRY0_WORKSPACE_ROOT"] = str(tmp_path)
    with patch("claude_agent_sdk.query", return_value=_scripted(messages)):
        return await SdkAgentExecutor().run(_inv(), {"configurable": {}, "_test_writer": lambda _e: None})


@pytest.mark.asyncio
async def test_old_sdk_result_without_error_fields_stays_success(tmp_path) -> None:
    out = await _run([_OldResult("rate limit mentioned in a successful answer")], tmp_path)
    assert out.is_error is False, "an SDK result without is_error must stay a success"
    assert out.error_code is None


@pytest.mark.asyncio
async def test_result_api_error_status_429_is_rate_limited(tmp_path) -> None:
    out = await _run([_Result(is_error=True, api_error_status=429)], tmp_path)
    assert out.is_error is True
    assert out.error_code == _RATE, "ResultMessage.api_error_status 429 must be the rate-limit code"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_result_api_error_status_auth(tmp_path, status: int) -> None:
    out = await _run([_Result(is_error=True, api_error_status=status)], tmp_path)
    assert out.error_code == _AUTH, f"api_error_status {status} must be auth rejected"


@pytest.mark.asyncio
async def test_result_error_text_is_the_fallback(tmp_path) -> None:
    out = await _run([_Result(is_error=True, errors=["usage limit reached"])], tmp_path)
    assert out.error_code == _QUOTA, "a result error with no status must use the text classifier"
    assert "usage limit reached" in out.error_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("word", "code"),
    [("rate_limit", _RATE), ("billing_error", _QUOTA), ("authentication_failed", _AUTH)],
)
async def test_assistant_errors(tmp_path, word: str, code: str) -> None:
    out = await _run([_Assistant(word), _OldResult("done")], tmp_path)
    assert out.is_error is True
    assert out.error_code == code, f"AssistantMessage.error {word!r} must be {code}, got {out.error_code}"


@pytest.mark.asyncio
async def test_rate_limit_event_rejected_is_rate_limited(tmp_path) -> None:
    out = await _run([_RateEvent("rejected")], tmp_path)
    assert out.is_error is True
    assert out.error_code == _RATE, "RateLimitEvent status rejected must be the rate-limit code"


@pytest.mark.asyncio
async def test_rate_limit_event_warning_is_not_a_failure(tmp_path) -> None:
    out = await _run([_RateEvent("allowed_warning"), _OldResult("done")], tmp_path)
    assert out.is_error is False, "a rate-limit warning must not fail the run"
    assert out.error_code is None


@pytest.mark.asyncio
async def test_real_result_message_status_is_classified(tmp_path) -> None:
    """The installed SDK's ResultMessage carries api_error_status. getattr still reads it."""
    from claude_agent_sdk import ResultMessage

    message = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="s1",
        total_cost_usd=0.0,
        usage={},
        result="",
        api_error_status=429,
    )
    out = await _run([message], tmp_path)
    assert out.error_code == _RATE, "a real ResultMessage with api_error_status 429 must be rate limited"
