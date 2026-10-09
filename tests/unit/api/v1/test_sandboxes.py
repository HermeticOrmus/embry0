"""Tests for GET /api/v1/sandboxes/active — docker ps wrapper for ops visibility."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config
from embry0.execution.runtime import DockerRuntime


@pytest.fixture
def app():
    config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True)
    return create_app(config)


@pytest.mark.asyncio
async def test_list_active_sandboxes_empty_when_runtime_absent(app):
    """When no runtime is on app.state, return an empty list (no 500)."""
    if hasattr(app.state, "runtime"):
        delattr(app.state, "runtime")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/sandboxes/active")
    assert resp.status_code == 200
    assert resp.json() == {"containers": [], "count": 0}


@pytest.mark.asyncio
async def test_list_active_sandboxes_parses_ps_output(app):
    """docker ps output is split into name/status/running_for fields."""
    mock_docker = MagicMock()
    mock_docker._build_base_cmd = MagicMock(return_value=["docker"])
    mock_docker.run_cmd = AsyncMock(
        return_value=(
            "abc123\tsandbox-abc\t2026-10-08 08:00:00 +0000 UTC\tUp 2 hours\t2 hours ago\n"
            "def456\tsandbox-def\t2026-10-08 09:55:00 +0000 UTC\tExited (0) 5 minutes ago\t5 minutes ago\n"
        )
    )
    app.state.runtime = DockerRuntime(mock_docker)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/sandboxes/active")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["containers"] == [
        {"name": "sandbox-abc", "status": "Up 2 hours", "running_for": "2 hours ago"},
        {
            "name": "sandbox-def",
            "status": "Exited (0) 5 minutes ago",
            "running_for": "5 minutes ago",
        },
    ]
    # Verify the ps command was built with the sandbox- filter and expected format.
    call_args = mock_docker.run_cmd.call_args
    cmd = call_args.args[0]
    assert "ps" in cmd
    assert "name=sandbox-" in cmd
    assert "{{.ID}}\t{{.Names}}\t{{.CreatedAt}}\t{{.Status}}\t{{.RunningFor}}" in cmd
    # include_stopped defaults to False → `-a` should NOT be in the command.
    assert "-a" not in cmd
