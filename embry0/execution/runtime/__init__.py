"""The container runtime seam (#268). See ``protocol.py``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from embry0.execution.runtime.docker import DockerRuntime
from embry0.execution.runtime.protocol import AgentRuntime
from embry0.execution.runtime.rootless import RootlessDockerRuntime

if TYPE_CHECKING:
    from embry0.config import Embry0Config

__all__ = ["AgentRuntime", "DockerRuntime", "RootlessDockerRuntime", "build_runtime"]


def build_runtime(config: Embry0Config) -> AgentRuntime:
    """The runtime this process uses, chosen once at startup by ``AGENT_RUNTIME``.

    The choice between implementations belongs here, so that no call site
    ever branches on which one it got. Raises ValueError for a combination
    that cannot be what the operator meant.
    """
    if config.agent_runtime == "rootless":
        # The socket only. Over TCP the daemon's API would listen in the
        # network namespace whose bridge gateway every sandbox can address.
        if not config.docker_host.startswith("unix://"):
            raise ValueError(
                "AGENT_RUNTIME=rootless needs DOCKER_HOST=unix://<the sandbox daemon's socket>, "
                f"got {config.docker_host or '(unset)'!r}; see docs/ops/provisioning.md"
            )
        if config.docker_tls_verify or config.docker_cert_path:
            raise ValueError(
                "AGENT_RUNTIME=rootless talks to its daemon over a unix socket; "
                "unset DOCKER_TLS_VERIFY and DOCKER_CERT_PATH (they belong to the DinD daemon)"
            )
    # Looked up at call time, not bound at import: the lifespan tests replace
    # DockerClient on its own module.
    from embry0.execution import docker_client

    client = docker_client.DockerClient(
        docker_host=config.docker_host,
        tls_verify=config.docker_tls_verify,
        cert_path=config.docker_cert_path,
    )
    if config.agent_runtime == "rootless":
        return RootlessDockerRuntime(client)
    return DockerRuntime(client)
