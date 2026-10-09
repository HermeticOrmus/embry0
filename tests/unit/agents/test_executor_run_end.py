"""The agent's successful result ends the SDK run (#501).

claude-code 2.1.292 holds an SDK run open after the final result until every
background shell exits (up to its 30-minute limit), then runs one more turn on
the completion notice. Measured offline on 2.1.292: a ``sleep 150`` started with
``run_in_background`` kept the run open 150 s after the agent's final answer,
then added a paid turn. The CLI it replaces stopped such shells 5 s after the
result. The executor ends the run at the successful result and closes the
stream, which is what stops the CLI.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from embry0.agents.executor import SdkAgentExecutor
from embry0.agents.invocation import AgentInvocation
from embry0.safety.policy import default_policy_for_agent


def _inv() -> AgentInvocation:
    return AgentInvocation(
        agent_type="developer",
        prompt="do it",
        system_prompt="",
        system_context="",
        model="claude-sonnet-4-6",
        tools=["Bash"],
        skills=[],
        mcp_servers={},
        max_turns=5,
        timeout_seconds=60,
        execution_mode="sdk",
        auth_mode="oauth",
        safety_policy=default_policy_for_agent("developer"),
        channel_config=None,
    )


def _result(total_cost_usd: float, text: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=total_cost_usd,
        usage={"input_tokens": 1000, "output_tokens": 100},
        result=text,
    )


class _Stream:
    """An SDK message stream that records whether the executor read past the result and closed it."""

    def __init__(self, messages: list[Any]) -> None:
        self._messages = list(messages)
        self.read = 0
        self.closed = False

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        if not self._messages:
            raise StopAsyncIteration
        self.read += 1
        return self._messages.pop(0)

    async def aclose(self) -> None:
        self.closed = True


async def _run(stream: _Stream, tmp_path) -> Any:
    with (
        patch.dict("os.environ", {"EMBRY0_WORKSPACE_ROOT": str(tmp_path)}),
        patch("claude_agent_sdk.query", return_value=stream),
    ):
        return await SdkAgentExecutor().run(_inv(), {"configurable": {}, "_test_writer": lambda _event: None})


async def test_the_final_result_ends_the_run_and_closes_the_stream(tmp_path) -> None:
    stream = _Stream(
        [
            AssistantMessage(content=[TextBlock(text="final answer")], model="claude-sonnet-4-6"),
            _result(0.0090, "final answer"),
            # What 2.1.292 sends 150 s later, when the background shell exits:
            AssistantMessage(content=[TextBlock(text="the background command finished")], model="claude-sonnet-4-6"),
            _result(0.0135, "the background command finished"),
        ]
    )
    out = await _run(stream, tmp_path)

    assert out.output == "final answer", f"the run continued past the agent's result: {out.output!r}"
    assert out.cost_usd == 0.0090, out.cost_usd
    assert stream.read == 2, f"read {stream.read} messages; the stream must stop at the result"
    assert stream.closed is True, "the stream was left open, so the SDK never stops the CLI"


async def test_an_error_result_still_reaches_the_closing_exception(tmp_path) -> None:
    """Only a successful result ends the run early; an error's text comes from what follows."""
    closing = "Claude Code returned an error result: Reached maximum number of turns (5) (exit code: 1)"

    class _Failing(_Stream):
        async def __anext__(self) -> Any:
            if not self._messages:
                raise Exception(closing)
            return await super().__anext__()

    stream = _Failing(
        [
            ResultMessage(
                subtype="error_max_turns",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=5,
                session_id="s1",
                total_cost_usd=0.01,
                usage={},
                errors=["Reached maximum number of turns (5)"],
            )
        ]
    )
    out = await _run(stream, tmp_path)

    assert out.is_error is True
    assert out.error_message == closing, out.error_message
    assert stream.closed is True, "the stream was left open after the closing exception"
