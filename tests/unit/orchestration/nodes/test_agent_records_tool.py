"""run_agent_node grants the records tools exactly when init_node recorded ``records_tool``."""

from __future__ import annotations

from typing import Any

import pytest

from embry0.execution.agent_runner import AgentOutput
from embry0.orchestration.nodes.agent import run_agent_node
from embry0.orchestration.records_tool import RECORDS_TOOLS


class _Runner:
    def __init__(self) -> None:
        self.config: dict[str, Any] = {}

    async def run(self, container: str, config: dict[str, Any], *args: Any, **kwargs: Any) -> AgentOutput:
        self.config = config
        return AgentOutput(agent_type="developer", output="done", cost_usd=0.0)


async def _invoke(state_extra: dict[str, Any], definition: dict[str, Any] | None = None) -> dict[str, Any]:
    runner = _Runner()
    await run_agent_node(
        state={"sandbox_container_id": "c1", **state_extra},
        agent_runner=runner,
        agent_type="developer",
        prompt="implement",
        agent_definition=definition,
        credentials={"api_key": "", "oauth_token": "oauth-xyz"},
        config={"configurable": {"job_id": "J"}},
    )
    return runner.config


@pytest.mark.asyncio
async def test_flagged_job_gets_the_tools_and_server() -> None:
    sent = await _invoke({"records_tool": True})
    assert set(RECORDS_TOOLS) <= set(sent["tools"]), sent["tools"]
    assert sent["mcp_servers"]["records"]["args"] == ["-m", "embry0.sandbox.records_mcp"]


@pytest.mark.asyncio
async def test_unflagged_job_gets_nothing() -> None:
    sent = await _invoke({})
    assert not set(RECORDS_TOOLS) & set(sent["tools"])
    assert "records" not in sent["mcp_servers"]


@pytest.mark.asyncio
async def test_a_pipeline_mcp_override_cannot_drop_the_grant() -> None:
    sent = await _invoke(
        {"records_tool": True, "pipeline_config": {"mcp_servers": {"developer": {"other": {"type": "stdio"}}}}},
        {"model": "claude-sonnet-5", "tools": ["Read"], "skills": [], "system_prompt": "", "mcp_servers": {}},
    )
    assert set(sent["mcp_servers"]) == {"other", "records"}
    assert sent["tools"][0] == "Read" and set(RECORDS_TOOLS) <= set(sent["tools"])
