"""Repo records as an invocation-scoped grant for every agent of a job.

``mcp__records__records_list`` and ``mcp__records__records_get`` are served by
an in-sandbox stdio MCP server that ships in every sandbox image
(``embry0/sandbox/records_mcp.py``). It reads ``EMBRY0_RECORDS_PROXY_URL`` and
the 0600 token file init_node delivers, and the records-proxy decides which
repository's records it may read: the job's own.

The tools are added after the invocation is resolved, so a pipeline's
``mcp_servers`` override cannot drop them, and only when init_node recorded
``records_tool`` in the job state: the records-proxy confirmed this job's
sandbox. A tool config that leaks anywhere else dead-ends at the proxy's 401.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from embry0.agents.invocation import AgentInvocation

RECORDS_MCP_SERVER: dict[str, dict[str, Any]] = {
    "records": {
        "type": "stdio",
        "command": "python3",
        "args": ["-m", "embry0.sandbox.records_mcp"],
    }
}
RECORDS_TOOLS: tuple[str, ...] = ("mcp__records__records_list", "mcp__records__records_get")


def grant_records_tool(invocation: AgentInvocation) -> AgentInvocation:
    """A copy of ``invocation`` with the records tools appended and their server registered."""
    tools = list(invocation.tools)
    tools.extend(t for t in RECORDS_TOOLS if t not in tools)
    return replace(invocation, tools=tools, mcp_servers={**invocation.mcp_servers, **RECORDS_MCP_SERVER})
