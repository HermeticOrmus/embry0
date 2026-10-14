"""init_node provisions the records tool only for a confirmed sandbox of a job with a repository."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

TOKEN = "a" * 43


def _mocks(*, records_url: str = "http://records-proxy:9108", confirmed: bool = True):
    sandbox_mgr = MagicMock()
    sandbox_mgr.create = AsyncMock(return_value=("container-abc", TOKEN))
    sandbox_mgr.destroy = AsyncMock()
    docker = MagicMock()
    docker.build_exec_cmd = MagicMock(side_effect=lambda cid, cmd, **kw: ["docker", "exec", cid, *cmd])
    docker.run_cmd = AsyncMock(return_value="")
    proxy_mgr = MagicMock()
    proxy_mgr.git_proxy_url = ""
    proxy_mgr.xai_proxy_url = ""
    proxy_mgr.context_proxy_url = ""
    proxy_mgr.records_proxy_url = records_url
    proxy_mgr.records_access = MagicMock(return_value=confirmed)
    return sandbox_mgr, docker, proxy_mgr


async def _run(state, sandbox_mgr, docker, proxy_mgr):
    import embry0.workflows.issue_to_pr.nodes as nodes_module
    from embry0.workflows.issue_to_pr.nodes import init_node

    orig = nodes_module.get_stream_writer
    nodes_module.get_stream_writer = lambda: lambda _e: None
    try:
        return await init_node(
            state=state,
            config={"configurable": {"sandbox_manager": sandbox_mgr, "docker": docker, "proxy_manager": proxy_mgr}},
        )
    finally:
        nodes_module.get_stream_writer = orig


def _token_writes(docker) -> list[str]:
    cmds = [" ".join(str(p) for p in c.args[0]) for c in docker.run_cmd.await_args_list]
    return [c for c in cmds if "records_proxy_token" in c]


@pytest.mark.asyncio
async def test_a_confirmed_sandbox_gets_the_records_tool() -> None:
    sandbox_mgr, docker, proxy_mgr = _mocks()
    update = await _run(
        {"job_id": "job-r1", "repo": "acme/widgets", "context": {"type": "none"}}, sandbox_mgr, docker, proxy_mgr
    )
    _, create_kwargs = sandbox_mgr.create.call_args
    assert create_kwargs["env"].get("EMBRY0_RECORDS_PROXY_URL") == "http://records-proxy:9108"
    proxy_mgr.records_access.assert_called_once_with("container-abc")
    writes = _token_writes(docker)
    assert writes and "chmod 600" in writes[0] and TOKEN in writes[0]
    assert update.get("records_tool") is True


@pytest.mark.asyncio
async def test_an_unconfirmed_sandbox_gets_no_tool() -> None:
    sandbox_mgr, docker, proxy_mgr = _mocks(confirmed=False)
    update = await _run(
        {"job_id": "job-r2", "repo": "acme/widgets", "context": {"type": "none"}}, sandbox_mgr, docker, proxy_mgr
    )
    assert _token_writes(docker) == []
    assert "records_tool" not in update


@pytest.mark.asyncio
async def test_a_job_without_a_repo_gets_nothing() -> None:
    sandbox_mgr, docker, proxy_mgr = _mocks()
    update = await _run({"job_id": "job-r3", "context": {"type": "none"}}, sandbox_mgr, docker, proxy_mgr)
    _, create_kwargs = sandbox_mgr.create.call_args
    assert "EMBRY0_RECORDS_PROXY_URL" not in create_kwargs["env"]
    assert "records_tool" not in update


@pytest.mark.asyncio
async def test_a_failed_token_write_degrades_without_failing_the_job() -> None:
    sandbox_mgr, docker, proxy_mgr = _mocks()

    async def run_cmd(cmd, **kw):
        if "records_proxy_token" in " ".join(str(p) for p in cmd):
            raise RuntimeError("exec failed")
        return ""

    docker.run_cmd = AsyncMock(side_effect=run_cmd)
    update = await _run(
        {"job_id": "job-r4", "repo": "acme/widgets", "context": {"type": "none"}}, sandbox_mgr, docker, proxy_mgr
    )
    assert update.get("current_stage") == "initialized"
    assert "records_tool" not in update


def test_the_state_key_is_declared() -> None:
    """An undeclared key is silently dropped by LangGraph's state merge."""
    from embry0.orchestration.state import JobState

    assert "records_tool" in JobState.__annotations__
