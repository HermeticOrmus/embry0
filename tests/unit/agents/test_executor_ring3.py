"""Unit tests for the Ring-3 hook plumbing in SdkAgentExecutor.

The SDK hook-support check itself is tested in test_sdk_support.py."""


# ---------------------------------------------------------------------------
# EMB-37 hotfix: hook-input tool_name extraction
# ---------------------------------------------------------------------------


def test_extract_hook_call_from_dict_input():
    """The SDK's PreToolUseHookInput is a TypedDict — a plain dict at runtime.
    getattr() on it always returned None, so tool_name arrived as '' at
    evaluate_policy: tool-scoped content rules never matched, and the EMB-37
    name check denied EVERY call ('tool '' is not in this agent's allowlist',
    observed on job-616393868ba6)."""
    from embry0.agents.executor import _extract_hook_call

    name, tool_input = _extract_hook_call(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t1"}
    )
    assert name == "Bash"
    assert tool_input == {"command": "ls"}


def test_extract_hook_call_from_object_input():
    from embry0.agents.executor import _extract_hook_call

    class _Obj:
        tool_name = "Read"
        tool_input = {"file_path": "/x"}

    name, tool_input = _extract_hook_call(_Obj())
    assert name == "Read"
    assert tool_input == {"file_path": "/x"}


def test_extract_hook_call_defends_malformed_input():
    from embry0.agents.executor import _extract_hook_call

    name, tool_input = _extract_hook_call({"tool_input": "not-a-dict"})
    assert name == ""
    assert tool_input == {}


# ---------------------------------------------------------------------------
# #497: hook support is checked where the hook is enforced, and fails closed
# ---------------------------------------------------------------------------


async def test_an_sdk_without_hook_support_never_reaches_query(tmp_path, monkeypatch):
    """An image whose SDK cannot carry the PreToolUse hook gets an error, not an unguarded run."""
    from unittest.mock import MagicMock

    from embry0.agents import executor as executor_module
    from embry0.agents.executor import SdkAgentExecutor
    from embry0.agents.invocation import AgentInvocation
    from embry0.agents.sdk_support import SdkSupportError
    from embry0.safety.policy import default_policy_for_agent

    def refuse() -> None:
        raise SdkSupportError("claude_agent_sdk.ClaudeAgentOptions declares no `hooks` option")

    query = MagicMock(side_effect=AssertionError("query() ran without the Ring-3 hook"))
    monkeypatch.setenv("EMBRY0_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(executor_module, "assert_sdk_supports_hooks", refuse)
    monkeypatch.setattr("claude_agent_sdk.query", query)

    invocation = AgentInvocation(
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
    out = await SdkAgentExecutor().run(invocation, {"configurable": {}, "_test_writer": lambda _event: None})

    assert query.call_count == 0, "query() must not run on an SDK without hook support"
    assert out.is_error is True
    assert "declares no `hooks` option" in out.error_message, out.error_message
    assert out.error_code == "ERR_SANDBOX_INIT", f"an unusable image must carry a code, got {out.error_code!r}"
