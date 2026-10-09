"""#268: every sandbox and proxy container embry0 starts is labelled.

On the sandbox daemon, `docker ps --filter label=embry0.managed=true` must
find every container embry0 started and nothing else, and a sandbox must name
its job, so that a reaper (or an operator) can select them by label rather
than by a name prefix alone.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

from embry0.execution.docker_client import DockerClient
from embry0.execution.sandbox_manager import SandboxManager

# Autouse: every sandbox on sandbox-restricted gets the isolation chain (#554).
from tests.unit.execution.test_sandbox_manager import _stub_isolation_unless_overridden  # noqa: F401


def _labels(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]


def test_a_sandbox_is_labelled_with_its_role_and_job():
    cmd = DockerClient().build_run_cmd(image="embry0-sandbox:latest", name="sandbox-job-7", job_id="job-7")
    assert _labels(cmd) == ["embry0.managed=true", "embry0.role=sandbox", "embry0.job_id=job-7"]
    assert cmd.index("--label") < cmd.index("embry0-sandbox:latest"), "labels must precede the image"


def test_a_proxy_is_labelled_with_its_role():
    cmd, env_file = DockerClient().build_run_proxy_cmd(
        name="git-proxy", image="embry0-proxy:latest", network="sandbox-restricted", env={"PROXY_TYPE": "git"}
    )
    os.unlink(env_file)
    assert _labels(cmd) == ["embry0.managed=true", "embry0.role=proxy"]
    assert cmd.index("--label") < cmd.index("embry0-proxy:latest")


async def test_sandbox_manager_labels_the_sandbox_with_its_job():
    docker = MagicMock(spec=DockerClient)
    docker.run_cmd = AsyncMock(return_value="container-abc123")
    docker.build_run_cmd = MagicMock(return_value=["docker", "run", "..."])
    proxy_mgr = MagicMock()
    proxy_mgr.enroll_sandbox = AsyncMock(return_value="tok")
    manager = SandboxManager(docker, proxy_manager=proxy_mgr)
    await manager.create("job-7", profile={"name": "default", "extra_networks": []}, repo="acme/app")
    assert docker.build_run_cmd.call_args.kwargs["job_id"] == "job-7"
