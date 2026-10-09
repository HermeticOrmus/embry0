"""#268: gVisor (runsc) is not offered under AGENT_RUNTIME=rootless.

Under rootless Docker, runsc runs only with --ignore-cgroups, which creates no
container cgroup at all: every per-sandbox memory, CPU and pids limit would be
dropped. Isolation under rootless is rootless + cap-drop + the egress and
isolation chains + per-sandbox limits; gVisor is revisited for the SaaS
runtime (#587). So it is refused at both gates:

- SandboxManager.create refuses a profile naming any OCI runtime other than
  runc, before anything exists;
- verify_daemon refuses to start on a daemon that registers runsc or runs
  anything other than runc by default (a default runtime would put every
  sandbox under it without any profile asking).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.execution.docker_client import DockerClient
from embry0.execution.runtime import RootlessDockerRuntime
from embry0.execution.runtime.rootless import RootlessDaemonError, rootless_daemon_problems
from embry0.execution.sandbox_manager import SandboxInitError, SandboxManager
from embry0.safety.error_codes import ErrorCode

# Autouse: every sandbox on sandbox-restricted gets the isolation chain (#554).
from tests.unit.execution.test_sandbox_manager import _stub_isolation_unless_overridden  # noqa: F401

SOCKET = "unix:///run/embry0-sandbox/docker.sock"

# The fields of `docker info --format '{{json .}}'` that matter, as a daemon
# provisioned by infra/rootless/provision-sandbox-daemon.sh reports them
# (Docker 29.9, 2026-10-09).
PROVISIONED: dict[str, Any] = {
    "SecurityOptions": ["name=seccomp,profile=builtin", "name=rootless", "name=cgroupns"],
    "CgroupVersion": "2",
    "MemoryLimit": True,
    "CpuCfsQuota": True,
    "PidsLimit": True,
    "DefaultRuntime": "runc",
    "Runtimes": {"io.containerd.runc.v2": {"path": "runc"}, "runc": {"path": "runc"}},
}


def _manager() -> tuple[SandboxManager, MagicMock]:
    docker = MagicMock(spec=DockerClient)
    docker.run_cmd = AsyncMock(return_value="container-abc123")
    docker.build_run_cmd = MagicMock(return_value=["docker", "run", "..."])
    docker.build_rm_cmd = MagicMock(return_value=["docker", "rm", "..."])
    proxy_mgr = MagicMock()
    proxy_mgr.enroll_sandbox = AsyncMock(return_value="tok")
    proxy_mgr.unenroll_sandbox = AsyncMock()
    return SandboxManager(docker, proxy_manager=proxy_mgr, runtime=RootlessDockerRuntime(docker)), docker


@pytest.mark.parametrize("runtime", ["runsc", "io.containerd.runsc.v1", "kata"])
async def test_a_profile_asking_for_another_oci_runtime_is_refused_before_anything_exists(runtime) -> None:
    manager, docker = _manager()
    profile = {"name": "gvisor", "runtime": runtime, "extra_networks": []}
    with pytest.raises(SandboxInitError) as info:
        await manager.create("job-1", profile=profile, repo="acme/app")
    assert info.value.error_code == ErrorCode.INCOHERENT_CONFIG
    assert "gVisor (runsc) is not offered under AGENT_RUNTIME=rootless" in str(info.value)
    docker.run_cmd.assert_not_awaited()


@pytest.mark.parametrize("runtime", [None, "", "runc"])
async def test_runc_or_no_runtime_still_creates_the_sandbox(runtime) -> None:
    manager, docker = _manager()
    profile: dict[str, Any] = {"name": "default", "extra_networks": []}
    if runtime is not None:
        profile["runtime"] = runtime
    container_id, _ = await manager.create("job-1", profile=profile, repo="acme/app")
    assert container_id == "container-abc123"


def test_the_provisioned_daemon_passes() -> None:
    assert rootless_daemon_problems(PROVISIONED) == []


@pytest.mark.parametrize(
    "change",
    [
        {"Runtimes": {**PROVISIONED["Runtimes"], "runsc": {"path": "/usr/bin/runsc"}}},
        {"Runtimes": {**PROVISIONED["Runtimes"], "gvisor": {"path": "/usr/local/bin/runsc"}}},
        {"DefaultRuntime": "runsc"},
        {"DefaultRuntime": "kata"},
    ],
    ids=["runsc-registered", "runsc-under-another-name", "runsc-default", "other-default"],
)
async def test_the_start_is_refused_on_a_daemon_offering_gvisor(change) -> None:
    info = {**PROVISIONED, **change}
    assert any("gVisor" in p or "default runtime" in p for p in rootless_daemon_problems(info)), (
        rootless_daemon_problems(info)
    )
    client = MagicMock(spec=DockerClient)
    client._build_base_cmd.return_value = ["docker", "--host", SOCKET]
    client.run_cmd = AsyncMock(return_value=json.dumps(info))
    with pytest.raises(RootlessDaemonError, match="refuses to start"):
        await RootlessDockerRuntime(client).verify_daemon()
