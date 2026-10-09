"""#268: a runtime that never hands a sandbox its daemon refuses every ask for one.

Under ``AGENT_RUNTIME=rootless`` the daemon's account owns every sandbox and
proxy, and a container it starts on the host network could rewrite the
iptables chains that isolate them. A profile with ``dind_enabled`` is refused
before anything exists, even for a repo an admin granted Docker access
(#522), and QA ``mode: dind`` is refused before its network or sandbox.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.execution.docker_client import DockerClient
from embry0.execution.runtime import DockerRuntime, RootlessDockerRuntime
from embry0.execution.sandbox_manager import SandboxInitError, SandboxManager
from embry0.safety.error_codes import ErrorCode
from embry0.workflows.qa.runtime_gate import dind_mode_refusal

# Autouse: every sandbox on sandbox-restricted gets the isolation chain (#554).
from tests.unit.execution.test_sandbox_manager import _stub_isolation_unless_overridden  # noqa: F401


class _GrantsEveryRepo:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def docker_access(self, repo: str) -> bool:
        self.asked.append(repo)
        return True


def _manager(runtime_cls: type[DockerRuntime]) -> tuple[SandboxManager, MagicMock, _GrantsEveryRepo]:
    docker = MagicMock(spec=DockerClient)
    docker.run_cmd = AsyncMock(return_value="container-abc123")
    docker.build_run_cmd = MagicMock(return_value=["docker", "run", "..."])
    docker.build_rm_cmd = MagicMock(return_value=["docker", "rm", "..."])
    proxy_mgr = MagicMock()
    proxy_mgr.enroll_sandbox = AsyncMock(return_value="tok")
    proxy_mgr.unenroll_sandbox = AsyncMock()
    grants = _GrantsEveryRepo()
    manager = SandboxManager(
        docker=docker,
        proxy_manager=proxy_mgr,
        docker_access_registry=grants,
        runtime=runtime_cls(docker),
    )
    return manager, docker, grants


async def test_rootless_refuses_a_dind_profile_even_with_a_grant() -> None:
    manager, docker, grants = _manager(RootlessDockerRuntime)
    with pytest.raises(SandboxInitError) as info:
        await manager.create("job-1", profile={"name": "qa-dind", "dind_enabled": True}, repo="acme/app")
    assert info.value.error_code == ErrorCode.INCOHERENT_CONFIG
    assert "'rootless' sandbox runtime never gives a sandbox one" in str(info.value)
    assert grants.asked == [], "the grant must not be read: no grant can open this daemon"
    docker.run_cmd.assert_not_awaited()
    docker.build_run_cmd.assert_not_called()


async def test_rootless_creates_a_sandbox_that_asks_for_no_daemon() -> None:
    manager, docker, _ = _manager(RootlessDockerRuntime)
    container_id, _ = await manager.create("job-1", profile={"name": "default", "extra_networks": []}, repo="acme/app")
    assert container_id == "container-abc123"
    env = docker.build_run_cmd.call_args.kwargs["env"]
    assert "DOCKER_HOST" not in env


async def test_dind_still_hands_a_granted_repo_its_daemon() -> None:
    manager, docker, grants = _manager(DockerRuntime)
    manager._resolve_dind_gateway_ip = staticmethod(lambda: "172.30.0.2")  # type: ignore[method-assign]  # noqa: SLF001
    await manager.create(
        "job-1", profile={"name": "qa-dind", "dind_enabled": True, "extra_networks": []}, repo="acme/app"
    )
    assert grants.asked == ["acme/app"]
    assert docker.build_run_cmd.call_args.kwargs["env"]["DOCKER_HOST"] == "tcp://dind:2376"


# --- QA mode: dind ------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "dind_profile"), [("dind", False), ("process", True), ("dind", True)], ids=["mode", "profile", "both"]
)
def test_qa_dind_is_refused_on_a_runtime_without_sandbox_docker_access(mode, dind_profile) -> None:
    runtime = SimpleNamespace(kind="rootless", sandbox_docker_access=False)
    refusal = dind_mode_refusal(runtime, mode=mode, dind_profile=dind_profile)
    assert refusal is not None and "mode: dind is not available on the 'rootless'" in refusal


@pytest.mark.parametrize("access", [True, False])
def test_qa_process_mode_runs_on_either_runtime(access) -> None:
    runtime = SimpleNamespace(kind="x", sandbox_docker_access=access)
    assert dind_mode_refusal(runtime, mode="process", dind_profile=False) is None


def test_qa_dind_runs_on_the_dind_runtime() -> None:
    assert dind_mode_refusal(DockerRuntime(MagicMock()), mode="dind", dind_profile=True) is None


async def test_init_qa_refuses_dind_mode_before_creating_anything() -> None:
    from embry0.workflows.qa.nodes import init_qa_node

    runtime = MagicMock(spec=RootlessDockerRuntime)
    runtime.kind = "rootless"
    runtime.sandbox_docker_access = False
    sandbox_mgr = MagicMock()
    sandbox_mgr.create = AsyncMock()
    profiles = MagicMock()
    profiles.get = AsyncMock(return_value={"name": "qa-jvm", "dind_enabled": False})
    repos = MagicMock()
    repos.get = AsyncMock(return_value={"forge": "github"})
    state = {"job_id": "job-1", "repo": "acme/app", "qa": {"qa_yaml_parsed": {"mode": "dind"}}}
    config = {
        "configurable": {
            "docker": MagicMock(),
            "runtime": runtime,
            "sandbox_manager": sandbox_mgr,
            "profiles_repo": profiles,
            "qa_minio_sandbox": MagicMock(),
            "qa_token_registry": MagicMock(),
            "repos_repo": repos,
        }
    }
    with pytest.raises(RuntimeError, match="ERR_INCOHERENT_CONFIG: QA mode: dind is not available"):
        await init_qa_node(state, config)
    runtime.create_network.assert_not_called()
    sandbox_mgr.create.assert_not_awaited()


async def test_a_dind_mode_subtask_is_refused_before_creating_anything() -> None:
    from embry0.workflows.qa.subtask_nodes import acquire_sandbox_node
    from tests.unit.workflows.qa.test_subtask_nodes import _make_acquire_config, _make_acquire_state, _resolved_app

    config = _make_acquire_config()
    runtime = MagicMock(spec=RootlessDockerRuntime)
    runtime.kind = "rootless"
    runtime.sandbox_docker_access = False
    config["configurable"]["runtime"] = runtime
    state = {**_make_acquire_state(), "resolved": _resolved_app("hub", mode="dind")}

    out = await acquire_sandbox_node(state, config)

    assert out["status"] == "infra_failure"
    assert "mode: dind is not available on the 'rootless'" in out["failure_summary"]
    runtime.create_network.assert_not_called()
    config["configurable"]["sandbox_manager"].create.assert_not_awaited()
