"""``AGENT_RUNTIME``, ``build_runtime``'s choice and ``RootlessDockerRuntime`` (#268)."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from embry0.config import Embry0Config
from embry0.execution.docker_client import DockerClient
from embry0.execution.runtime import AgentRuntime, DockerRuntime, RootlessDockerRuntime, build_runtime
from embry0.execution.runtime.rootless import RootlessDaemonError, rootless_daemon_problems

SOCKET = "unix:///run/embry0-sandbox/docker.sock"

# `docker info --format '{{json .}}'` fields from a provisioned rootless daemon
# (infra/rootless/provision-sandbox-daemon.sh, Docker 29.9, 2026-10-09).
ROOTLESS_INFO: dict[str, Any] = {
    "SecurityOptions": ["name=seccomp,profile=builtin", "name=rootless", "name=cgroupns"],
    "CgroupVersion": "2",
    "CgroupDriver": "systemd",
    "MemoryLimit": True,
    "CpuCfsQuota": True,
    "PidsLimit": True,
}


def _config(**overrides: Any) -> Embry0Config:
    return Embry0Config(_env_file=None, **overrides)


def _client(info: dict[str, Any] | str) -> MagicMock:
    client = MagicMock(spec=DockerClient)
    client._build_base_cmd.return_value = ["docker", "--host", SOCKET]
    client.run_cmd = AsyncMock(return_value=info if isinstance(info, str) else json.dumps(info))
    return client


# --- the switch -------------------------------------------------------------


def test_the_default_runtime_is_still_dind():
    config = _config(docker_host="tcp://dind:2376", docker_tls_verify=True, docker_cert_path="/certs/client")
    assert config.agent_runtime == "dind"
    runtime = build_runtime(config)
    assert type(runtime) is DockerRuntime, "an unset AGENT_RUNTIME must keep the DinD runtime"
    assert runtime.sandbox_docker_access is True


def test_agent_runtime_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "rootless")
    assert Embry0Config(_env_file=None).agent_runtime == "rootless"


def test_an_unknown_agent_runtime_is_refused():
    with pytest.raises(ValidationError, match="agent_runtime"):
        _config(agent_runtime="host")


def test_rootless_builds_the_rootless_runtime_on_the_configured_socket():
    runtime = build_runtime(_config(agent_runtime="rootless", docker_host=SOCKET))
    assert isinstance(runtime, RootlessDockerRuntime)
    assert isinstance(runtime, AgentRuntime)
    assert runtime.kind == "rootless"
    assert runtime.sandbox_docker_access is False
    # No TLS flags: the socket's file mode is the access control.
    assert runtime.client._build_base_cmd() == ["docker", "--host", SOCKET]  # noqa: SLF001


@pytest.mark.parametrize("host", ["", "tcp://127.0.0.1:2376", "ssh://sandbox@host"])
def test_rootless_refuses_anything_but_a_unix_socket(host):
    with pytest.raises(ValueError, match="DOCKER_HOST=unix://"):
        build_runtime(_config(agent_runtime="rootless", docker_host=host))


@pytest.mark.parametrize(
    "tls", [{"docker_tls_verify": True}, {"docker_cert_path": "/certs/client"}], ids=["verify", "certs"]
)
def test_rootless_refuses_the_dind_tls_settings(tls):
    with pytest.raises(ValueError, match="DOCKER_TLS_VERIFY and DOCKER_CERT_PATH"):
        build_runtime(_config(agent_runtime="rootless", docker_host=SOCKET, **tls))


# --- the daemon check -------------------------------------------------------


def test_a_provisioned_rootless_daemon_has_no_problems():
    assert rootless_daemon_problems(ROOTLESS_INFO) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"SecurityOptions": ["name=seccomp,profile=builtin", "name=cgroupns"]}, "not rootless"),
        ({"SecurityOptions": ["name=seccomp,profile=rootless-ish"]}, "not rootless"),
        ({"CgroupVersion": "1"}, "cgroup v1"),
        ({"MemoryLimit": False}, "memory limits"),
        ({"CpuCfsQuota": False}, "CPU limits"),
        ({"PidsLimit": None}, "process limits"),
    ],
)
def test_each_missing_guarantee_is_named(change, expected):
    problems = rootless_daemon_problems({**ROOTLESS_INFO, **change})
    assert len(problems) == 1 and expected in problems[0], problems


async def test_verify_daemon_passes_a_rootless_daemon():
    client = _client(ROOTLESS_INFO)
    await RootlessDockerRuntime(client).verify_daemon()
    assert client.run_cmd.await_args.args[0] == ["docker", "--host", SOCKET, "info", "--format", "{{json .}}"]


async def test_verify_daemon_refuses_the_hosts_own_daemon():
    rootful = {**ROOTLESS_INFO, "SecurityOptions": ["name=seccomp,profile=builtin", "name=cgroupns"]}
    with pytest.raises(RootlessDaemonError, match="never be pointed at the host's own daemon"):
        await RootlessDockerRuntime(_client(rootful)).verify_daemon()


@pytest.mark.parametrize("reply", ["", "not json", "[]"])
async def test_verify_daemon_refuses_an_unreadable_answer(reply):
    with pytest.raises(RootlessDaemonError):
        await RootlessDockerRuntime(_client(reply)).verify_daemon()


async def test_verify_daemon_refuses_an_unreachable_daemon():
    client = _client(ROOTLESS_INFO)
    client.run_cmd.side_effect = RuntimeError("Cannot connect to the Docker daemon")
    with pytest.raises(RootlessDaemonError, match="Cannot connect"):
        await RootlessDockerRuntime(client).verify_daemon()


async def test_the_dind_runtime_has_nothing_to_verify():
    client = _client("")
    await DockerRuntime(client).verify_daemon()
    client.run_cmd.assert_not_awaited()


# --- production wiring ------------------------------------------------------


class _StopLifespan(BaseException):
    """Sentinel past the call under test."""


async def test_startup_refuses_a_rootless_runtime_on_the_wrong_daemon_before_using_it(monkeypatch):
    """The lifespan must call verify_daemon, and before any image is built on the daemon."""
    import asyncpg

    from embry0.api.app import create_app, lifespan
    from embry0.storage.database import DatabasePool

    database_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://embry0:embry0@localhost:5432/embry0_unit_api_test",
    )
    try:
        conn = await asyncpg.connect(database_url)
        await conn.close()
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")

    used: list[str] = []
    rootful = {**ROOTLESS_INFO, "SecurityOptions": ["name=seccomp,profile=builtin"]}
    monkeypatch.setattr("embry0.observability.configure_structlog", lambda **kw: None)
    monkeypatch.setattr("embry0.execution.docker_client.DockerClient", lambda **kw: _client(rootful))

    def _image_manager(*args, **kwargs):
        used.append("image manager built")
        raise _StopLifespan

    monkeypatch.setattr("embry0.execution.image_manager.SandboxImageManager", _image_manager)

    pools: list[DatabasePool] = []
    real_pool = DatabasePool

    def _recording_pool(url):
        pool = real_pool(url)
        pools.append(pool)
        return pool

    monkeypatch.setattr("embry0.api.app.DatabasePool", _recording_pool)
    app = create_app(
        _config(
            database_url=database_url,
            auth_dev_mode=True,
            webhook_dev_mode=True,
            api_key="",
            github_webhook_secret="test-secret",
            agent_runtime="rootless",
            docker_host=SOCKET,
        )
    )
    try:
        with pytest.raises(RootlessDaemonError, match="not rootless"):
            async with lifespan(app):
                pass
    finally:
        for pool in pools:
            await pool.close()
    assert used == [], "the daemon was used before it was verified"
