"""The shared Maven volume is handed to the agent user by a real daemon (#453).

The unit tests pin the command. This runs it: #453 shipped because the only
test of the ownership step asserted a flag on a mocked Docker client, and the
real command needed a capability the sandbox drops.

Requires: a Docker daemon and the JVM dev sandbox image
(``docker compose --profile images build dev-jvm-python-image-builder``).
"""

import subprocess
import uuid

import pytest

from embry0.execution.docker_client import DockerClient
from embry0.execution.runtime import DockerRuntime

IMAGE = "embry0-sandbox-dev-jvm-python:latest"
MOUNT = "/home/agent/.m2"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


@pytest.fixture
def scratch_volume():
    if _docker("image", "inspect", IMAGE).returncode != 0:
        pytest.skip(f"{IMAGE} is not built on this daemon")
    name = f"embry0-maven-test-{uuid.uuid4().hex[:8]}"
    assert _docker("volume", "create", name).returncode == 0
    yield name
    _docker("volume", "rm", "-f", name)


def _as_sandbox(volume: str, *command: str, user: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``command`` the way a sandbox runs: every capability dropped."""
    who = ["--user", user] if user else []
    return _docker(
        "run", "--rm", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges", *who,
        "-v", f"{volume}:{MOUNT}", "--entrypoint", command[0], IMAGE, *command[1:],
    )  # fmt: skip


@pytest.mark.requires_docker
def test_the_owner_container_makes_the_cache_writable_where_the_sandbox_cannot(scratch_volume: str):
    # A fresh named volume is root-owned: the agent cannot write it, and root
    # in a sandbox cannot fix that. This is #453, and why the helper exists.
    assert _as_sandbox(scratch_volume, "touch", f"{MOUNT}/probe").returncode != 0
    refused = _as_sandbox(scratch_volume, "chown", "agent:agent", MOUNT, user="root")
    assert refused.returncode != 0 and "Operation not permitted" in refused.stderr

    owner = DockerRuntime(DockerClient()).chown_volume_cmd(scratch_volume, MOUNT, image=IMAGE, owner="agent:agent")
    assert scratch_volume in " ".join(owner), "the test must not touch the real cache volume"
    handed_over = subprocess.run(owner, capture_output=True, text=True, timeout=120)
    assert handed_over.returncode == 0, handed_over.stderr

    written = _as_sandbox(scratch_volume, "touch", f"{MOUNT}/probe")
    assert written.returncode == 0, written.stderr
    # A second sandbox on the now agent-owned volume: the step is repeatable.
    assert subprocess.run(owner, capture_output=True, text=True, timeout=120).returncode == 0
