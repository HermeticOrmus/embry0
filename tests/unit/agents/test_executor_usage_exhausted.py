"""#466: an account whose usage window is spent is ERR_PROVIDER_QUOTA_EXHAUSTED,
with the provider's own sentence and its reset — not a rate limit that reads
"Claude Code returned an error result: success".

The SDK message classes are the installed ones. The sandbox image runs SDK
0.1.81, whose AssistantMessage, ResultMessage and RateLimitInfo have the same
fields; the stream below mirrors what CLI 2.1.x emits when it runs out of
usage: a synthetic assistant message flagged ``rate_limit``, an ``is_error``
result whose subtype is ``success``, then the SDK's closing exception.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo

from embry0.agents.executor import SdkAgentExecutor
from embry0.agents.invocation import AgentInvocation
from embry0.safety.policy import default_policy_for_agent

QUOTA = "ERR_PROVIDER_QUOTA_EXHAUSTED"
RATE = "ERR_PROVIDER_RATE_LIMITED"

# Verbatim from production event logs.
OUT_OF_USAGE_DAYS = "You're out of extra usage · resets Oct 5, 12am (UTC)"  # 2026-10-02 05:15 UTC
OUT_OF_USAGE_HOURS = "You're out of extra usage · resets 12:50am (UTC)"  # 2026-09-28 21:11 UTC
CLOSING_EXCEPTION = "Claude Code returned an error result: success"

PRODUCTION_CASES = [
    pytest.param(
        OUT_OF_USAGE_DAYS,
        datetime(2026, 10, 2, 5, 15, 18, tzinfo=UTC),
        datetime(2026, 10, 5, 0, 0, tzinfo=UTC),
        id="multi-day-reset",
    ),
    pytest.param(
        OUT_OF_USAGE_HOURS,
        datetime(2026, 9, 28, 21, 11, tzinfo=UTC),
        datetime(2026, 9, 29, 0, 50, tzinfo=UTC),
        id="same-night-reset",
    ),
]


def _inv() -> AgentInvocation:
    return AgentInvocation(
        agent_type="triage",
        prompt="triage this",
        system_prompt="",
        system_context="",
        model="claude-sonnet-4-6",
        tools=["Read"],
        skills=[],
        mcp_servers={},
        max_turns=5,
        timeout_seconds=60,
        execution_mode="sdk",
        auth_mode="oauth",
        safety_policy=default_policy_for_agent("triage"),
        channel_config=None,
    )


def _synthetic(text: str, *, error: str = "rate_limit") -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>", error=error)  # type: ignore[arg-type]


def _error_result(text: str, *, api_error_status: int | None = 429) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="s1",
        total_cost_usd=0.0,
        usage={},
        result=text,
        api_error_status=api_error_status,
    )


def _rate_event(**info: Any) -> RateLimitEvent:
    return RateLimitEvent(rate_limit_info=RateLimitInfo(**info), uuid="u1", session_id="s1")


async def _stream(messages: list[Any], *, closing: Exception | None = None):
    for message in messages:
        yield message
    if closing is not None:
        raise closing


async def _run(messages: list[Any], tmp_path, monkeypatch, *, now: datetime | None = None, closing=None) -> Any:
    monkeypatch.setenv("EMBRY0_WORKSPACE_ROOT", str(tmp_path))
    executor = SdkAgentExecutor(clock=lambda: now) if now is not None else SdkAgentExecutor()
    with patch("claude_agent_sdk.query", return_value=_stream(messages, closing=closing)):
        return await executor.run(_inv(), {"configurable": {}, "_test_writer": lambda _e: None})


# --- the production sequence ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("sentence", "now", "reset"), PRODUCTION_CASES)
async def test_production_sequence_keeps_the_sentence_and_code(tmp_path, monkeypatch, sentence, now, reset) -> None:
    """The whole stream a spent account produces, closing exception included."""
    out = await _run(
        [_synthetic(sentence), _error_result(sentence)],
        tmp_path,
        monkeypatch,
        closing=Exception(CLOSING_EXCEPTION),
    )
    assert out.is_error is True
    assert out.error_code == QUOTA, f"a spent usage window must be {QUOTA}, got {out.error_code}"
    assert out.error_message == sentence, f"the run's error must be the provider's sentence, got {out.error_message!r}"
    assert "error result: success" not in out.error_message


@pytest.mark.asyncio
@pytest.mark.parametrize(("sentence", "now", "reset"), PRODUCTION_CASES)
async def test_production_sequence_parses_the_reset(tmp_path, monkeypatch, sentence, now, reset) -> None:
    out = await _run(
        [_synthetic(sentence), _error_result(sentence)],
        tmp_path,
        monkeypatch,
        now=now,
        closing=Exception(CLOSING_EXCEPTION),
    )
    assert out.provider_resets_at == reset, f"expected reset {reset}, got {out.provider_resets_at}"


# --- each shape on its own ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("sentence", "now", "reset"), PRODUCTION_CASES)
async def test_assistant_rate_limit_flag_with_usage_text(tmp_path, monkeypatch, sentence, now, reset) -> None:
    """AssistantMessage.error == "rate_limit" is the same flag a transient 429 gets; the text decides."""
    out = await _run([_synthetic(sentence)], tmp_path, monkeypatch, now=now)
    assert out.error_code == QUOTA, f"assistant rate_limit + out-of-usage text must be {QUOTA}, got {out.error_code}"
    assert out.error_message == sentence, f"got {out.error_message!r}"
    assert out.provider_resets_at == reset


@pytest.mark.asyncio
@pytest.mark.parametrize(("sentence", "now", "reset"), PRODUCTION_CASES)
async def test_error_result_with_usage_text(tmp_path, monkeypatch, sentence, now, reset) -> None:
    """An is_error ResultMessage with api_error_status 429: the usage wording outranks the status."""
    out = await _run([_error_result(sentence)], tmp_path, monkeypatch, now=now)
    assert out.error_code == QUOTA, f"is_error result + out-of-usage text must be {QUOTA}, got {out.error_code}"
    assert out.error_message == sentence, f"got {out.error_message!r}"
    assert out.provider_resets_at == reset


@pytest.mark.asyncio
async def test_rate_limit_info_rejecting_a_named_window(tmp_path, monkeypatch) -> None:
    """Structured data: a rejected seven_day window with resets_at (Unix seconds)."""
    reset = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    out = await _run(
        [_rate_event(status="rejected", rate_limit_type="seven_day", resets_at=int(reset.timestamp()))],
        tmp_path,
        monkeypatch,
    )
    assert out.is_error is True
    assert out.error_code == QUOTA, f"a rejected named window must be {QUOTA}, got {out.error_code}"
    assert out.provider_resets_at == reset, f"got {out.provider_resets_at}"
    assert "seven_day" in out.error_message


@pytest.mark.asyncio
async def test_rate_limit_info_rejected_while_extra_usage_serves_is_not_a_failure(tmp_path, monkeypatch) -> None:
    """The window is spent but extra usage is allowed: the CLI keeps going, so must the run."""
    out = await _run(
        [
            _rate_event(
                status="rejected", rate_limit_type="five_hour", resets_at=1_790_000_000, overage_status="allowed"
            ),
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s1",
                total_cost_usd=0.1,
                usage={},
                result="done",
            ),
        ],
        tmp_path,
        monkeypatch,
    )
    assert out.is_error is False, "a run still served from extra usage must not be marked failed"
    assert out.error_code is None


@pytest.mark.asyncio
async def test_rate_limit_info_with_extra_usage_also_spent_takes_the_earlier_reset(tmp_path, monkeypatch) -> None:
    window = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
    overage = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    out = await _run(
        [
            _rate_event(
                status="rejected",
                rate_limit_type="seven_day",
                resets_at=int(window.timestamp()),
                overage_status="rejected",
                overage_resets_at=int(overage.timestamp()),
            )
        ],
        tmp_path,
        monkeypatch,
    )
    assert out.error_code == QUOTA
    assert out.provider_resets_at == overage, "the account is usable again at the earlier of the two resets"


@pytest.mark.asyncio
async def test_structured_reset_outranks_the_one_in_the_text(tmp_path, monkeypatch) -> None:
    structured = datetime(2026, 10, 5, 0, 7, tzinfo=UTC)
    out = await _run(
        [
            _rate_event(status="rejected", rate_limit_type="overage", resets_at=int(structured.timestamp())),
            _synthetic(OUT_OF_USAGE_DAYS),
            _error_result(OUT_OF_USAGE_DAYS),
        ],
        tmp_path,
        monkeypatch,
        now=datetime(2026, 10, 2, 5, 15, tzinfo=UTC),
        closing=Exception(CLOSING_EXCEPTION),
    )
    assert out.error_code == QUOTA
    assert out.provider_resets_at == structured, "rate_limit_info's resets_at must win over the parsed text"
    assert out.error_message == OUT_OF_USAGE_DAYS, "the provider's sentence must replace the composed text"


# --- the sentence after earlier assistant text -----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        f"I'll apply the schema change. Now the migration: {OUT_OF_USAGE_DAYS}",
        f"Step 1 is done.\n\n{OUT_OF_USAGE_DAYS}",
    ],
    ids=["same-line", "own-line"],
)
async def test_sentence_after_earlier_text(tmp_path, monkeypatch, text) -> None:
    now = datetime(2026, 10, 2, 5, 15, tzinfo=UTC)
    for messages in ([_synthetic(text)], [_error_result(text)]):
        out = await _run(messages, tmp_path, monkeypatch, now=now)
        assert out.error_code == QUOTA, f"{type(messages[0]).__name__}: expected {QUOTA}, got {out.error_code}"
        assert out.error_message == OUT_OF_USAGE_DAYS, (
            f"{type(messages[0]).__name__}: the kept text must be the provider's sentence, got {out.error_message!r}"
        )
        assert out.provider_resets_at == datetime(2026, 10, 5, 0, 0, tzinfo=UTC)


# --- a real rate limit stays one ---------------------------------------------------

TRANSIENT_429 = "API Error: Request rejected (429) · this may be a temporary capacity issue."


@pytest.mark.asyncio
async def test_assistant_rate_limit_without_usage_wording_stays_rate_limited(tmp_path, monkeypatch) -> None:
    out = await _run(
        [_synthetic(TRANSIENT_429), _error_result(TRANSIENT_429)],
        tmp_path,
        monkeypatch,
        closing=Exception(CLOSING_EXCEPTION),
    )
    assert out.error_code == RATE, f"a 429 with no usage wording must stay {RATE}, got {out.error_code}"
    assert out.error_message == TRANSIENT_429, f"the provider's text must be kept, got {out.error_message!r}"
    assert out.provider_resets_at is None, "a rate limit carries no usage-window reset"


@pytest.mark.asyncio
async def test_bare_429_result_stays_rate_limited(tmp_path, monkeypatch) -> None:
    out = await _run([_error_result("", api_error_status=429)], tmp_path, monkeypatch)
    assert out.error_code == RATE, f"api_error_status 429 alone must stay {RATE}, got {out.error_code}"
    assert out.provider_resets_at is None


@pytest.mark.asyncio
async def test_rate_limit_info_rejection_naming_no_window_stays_rate_limited(tmp_path, monkeypatch) -> None:
    out = await _run([_rate_event(status="rejected")], tmp_path, monkeypatch)
    assert out.error_code == RATE, f"a rejection that names no window must stay {RATE}, got {out.error_code}"
    assert out.provider_resets_at is None


# --- other error results keep their wording ----------------------------------------


@pytest.mark.asyncio
async def test_a_turn_cap_error_result_keeps_the_sdk_wording(tmp_path, monkeypatch) -> None:
    """Only an account failure replaces the SDK's closing exception; a turn cap reads as before."""
    closing = "Claude Code returned an error result: Reached maximum number of turns (40)"
    turn_cap = ResultMessage(
        subtype="error_max_turns",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=40,
        session_id="s1",
        total_cost_usd=0.5,
        usage={},
        errors=["Reached maximum number of turns (40)"],
    )
    out = await _run([turn_cap], tmp_path, monkeypatch, closing=Exception(closing))
    assert out.error_code == "ERR_AGENT_MAX_TURNS"
    assert out.error_message == closing, f"a non-account error must keep the SDK's wording, got {out.error_message!r}"
    assert out.provider_resets_at is None
