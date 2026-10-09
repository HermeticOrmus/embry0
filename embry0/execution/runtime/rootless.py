"""``AgentRuntime`` over a dedicated rootless Docker daemon (#268, ``AGENT_RUNTIME=rootless``).

The daemon runs under rootlesskit as an unprivileged account on the deploy
host, set up by ``infra/rootless/provision-sandbox-daemon.sh``
(docs/ops/provisioning.md). It is the Docker CLI on another ``DOCKER_HOST``,
so everything ``DockerRuntime`` does works unchanged: the sandbox bridges,
their iptables and ``docker run --network host`` all sit in rootlesskit's
network namespace, which is where the egress and isolation helpers write.

What differs is what the orchestrator may assume about the daemon:

- It must really be the rootless one. A socket that turns out to be the
  host's own daemon would hand the orchestrator root over everything else on
  the host, so ``verify_networks`` refuses the start unless the daemon
  reports itself rootless.
- It must be able to enforce per-sandbox limits. A rootless daemon without
  cgroup v2 delegation accepts ``--memory``, ``--cpus`` and ``--pids-limit``
  and drops them with a warning, so the start is refused unless the daemon
  reports all three enforceable.
- No sandbox is ever handed it (``sandbox_docker_access`` is False): the
  account that owns it owns every sandbox and proxy, and a container it starts
  on the host network can rewrite the very iptables chains that isolate them.
- Every sandbox runs under runc. gVisor (``runsc``) is not offered: under
  rootless Docker it runs only with ``--ignore-cgroups``, which drops every
  per-sandbox memory, CPU and pids limit. Isolation here is rootless +
  cap-drop + the egress and isolation chains + per-sandbox limits; gVisor is
  revisited for the SaaS runtime (#587). A profile naming another runtime is
  refused, and so is a daemon that registers runsc or defaults to anything
  but runc, since a default runtime applies to every sandbox unasked.
"""

from __future__ import annotations

import json
from typing import Any

from embry0.execution.runtime.docker import DockerRuntime
from embry0.safety.error_codes import ErrorCode

# The one OCI runtime sandboxes run under on this daemon.
_RUNC = "runc"
_GVISOR_REFUSAL = (
    "gVisor (runsc) is not offered under AGENT_RUNTIME=rootless: under rootless Docker it runs "
    "only with --ignore-cgroups, which drops every per-sandbox memory, CPU and pids limit. "
    "Sandboxes run under runc; gVisor is revisited for the SaaS runtime (#587)"
)

# What `docker info` must report for this runtime to be used at all.
_REQUIRED_CAPABILITIES = {
    "MemoryLimit": "memory limits (--memory)",
    "CpuCfsQuota": "CPU limits (--cpus)",
    "PidsLimit": "process limits (--pids-limit)",
}


class RootlessDaemonError(RuntimeError):
    """The configured daemon is not a rootless daemon that can hold sandboxes."""

    # A deployment configured against the wrong daemon; it refuses the start.
    error_code = ErrorCode.INCOHERENT_CONFIG


def rootless_daemon_problems(info: dict[str, Any]) -> list[str]:
    """Why the daemon described by ``docker info`` cannot run sandboxes rootless. Empty when it can."""
    problems: list[str] = []
    options = [str(opt) for opt in info.get("SecurityOptions") or []]
    if not any("name=rootless" in opt.split(",") for opt in options):
        problems.append(
            "the daemon is not rootless (no name=rootless in its security options); "
            "AGENT_RUNTIME=rootless must never be pointed at the host's own daemon"
        )
    if str(info.get("CgroupVersion") or "") != "2":
        problems.append(f"the daemon is on cgroup v{info.get('CgroupVersion') or '?'}, not v2")
    for key, what in _REQUIRED_CAPABILITIES.items():
        if info.get(key) is not True:
            problems.append(f"the daemon cannot enforce {what}; is cgroup delegation set up?")
    default = str(info.get("DefaultRuntime") or _RUNC)
    if default != _RUNC:
        problems.append(f"the daemon's default runtime is {default!r}, not runc; every sandbox would run under it")
    runtimes = info.get("Runtimes") or {}
    offered = sorted(
        str(name)
        for name, spec in (runtimes.items() if isinstance(runtimes, dict) else [])
        if "runsc" in str(name) or "runsc" in str((spec or {}).get("path", "") if isinstance(spec, dict) else "")
    )
    if offered:
        problems.append(f"the daemon registers {offered}: {_GVISOR_REFUSAL}; remove it from daemon.json")
    return problems


class RootlessDockerRuntime(DockerRuntime):
    """Drives the dedicated rootless sandbox daemon through ``DockerClient``."""

    kind = "rootless"
    sandbox_docker_access = False

    def oci_runtime_refusal(self, runtime: str) -> str | None:
        if not runtime or runtime == _RUNC:
            return None
        return f"sandbox profile asks for the {runtime!r} runtime. {_GVISOR_REFUSAL}."

    async def verify_daemon(self) -> None:
        """Refuse unless the daemon is rootless and can enforce per-sandbox limits."""
        try:
            raw = await self.client.run_cmd(self._cmd("info", "--format", "{{json .}}"), timeout=15)
            info = json.loads(raw or "{}")
        except Exception as exc:  # noqa: BLE001 — an unreadable daemon is a refused start, with its cause
            raise RootlessDaemonError(f"cannot read the rootless sandbox daemon's info: {exc}") from exc
        problems = rootless_daemon_problems(info if isinstance(info, dict) else {})
        if problems:
            raise RootlessDaemonError(
                "AGENT_RUNTIME=rootless refuses to start: "
                + "; ".join(problems)
                + ". See docs/ops/provisioning.md (A rootless sandbox daemon)."
            )
