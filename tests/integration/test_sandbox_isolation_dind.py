"""Integration: the isolation chain on a real Docker daemon (#554).

Builds a stand-in of ``sandbox-restricted`` (masquerade off) with two
"proxies" and two "sandboxes" that each listen on a port, plus a per-job
network and a listener on the daemon's own network namespace, then checks
which connections the chain lets through. Uses its own network, container
and chain names, so it never touches ``sandbox-restricted`` itself.

Opt-in. Point ``EMBRY0_ISOLATION_PROBE_DOCKER_HOST`` at a THROWAWAY daemon,
e.g. a privileged ``docker:27-dind`` started for the purpose::

    docker run -d --privileged --name isolation-probe -e DOCKER_TLS_CERTDIR= docker:27-dind
    EMBRY0_ISOLATION_PROBE_DOCKER_HOST=tcp://<its ip>:2375 \\
        pytest tests/integration/test_sandbox_isolation_dind.py -m requires_docker
    docker rm -fv isolation-probe    # -v: the image keeps /var/lib/docker in a volume

Never a live embry0 DinD: the test writes iptables rules into the daemon's
network namespace. The daemon needs ``alpine:3.20`` and builds the iptables
helper image on first use (``apk add`` needs internet).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from embry0.execution.docker_client import DockerClient
from embry0.execution.runtime.egress import HELPER_IMAGE
from embry0.execution.runtime.isolation import IsolationError, SandboxIsolation, bridge_interface

HOST = os.environ.get("EMBRY0_ISOLATION_PROBE_DOCKER_HOST", "")

pytestmark = [
    pytest.mark.requires_docker,
    pytest.mark.skipif(not HOST, reason="set EMBRY0_ISOLATION_PROBE_DOCKER_HOST to a throwaway daemon"),
]

_IMAGE = "alpine:3.20"
_SANDBOX_PORT = 8080
_PROXY_PORT = 9101
_APP_PORT = 3000
_GATEWAY_PORT = 18554


def _docker(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", "--host", HOST, *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result


@dataclass
class Probe:
    tag: str
    chain: str
    restricted: str
    qa_net: str
    bridge: str = ""
    gateway: str = ""
    containers: list[str] = field(default_factory=list)

    def name(self, role: str) -> str:
        return f"isoprobe-{self.tag}-{role}"

    def listen(self, role: str, port: int, *run_args: str) -> str:
        name = self.name(role)
        _docker(
            "run",
            "-d",
            "--name",
            name,
            *run_args,
            _IMAGE,
            "sh",
            "-c",
            f"exec nc -lk -p {port} -e echo {role}-says-hi",
        )
        self.containers.append(name)
        return name

    def ip(self, container: str, network: str | None = None) -> str:
        net = network or self.restricted
        fmt = f'{{{{with index .NetworkSettings.Networks "{net}"}}}}{{{{.IPAddress}}}}{{{{end}}}}'
        return _docker("inspect", "--format", fmt, container).stdout.strip()

    def reaches(self, src: str, address: str, port: int) -> bool:
        out = _docker("exec", src, "timeout", "8", "nc", "-w", "3", address, str(port), check=False)
        return "-says-hi" in out.stdout

    def isolation(self) -> SandboxIsolation:
        return SandboxIsolation(DockerClient(docker_host=HOST), network=self.restricted, chain=self.chain)

    def proxies(self) -> list[str]:
        return [self.name("git-proxy"), self.name("minio-proxy")]

    def iptables(self, script: str) -> str:
        return _docker(
            "run",
            "--rm",
            "--network",
            "host",
            "--cap-add",
            "NET_ADMIN",
            "--entrypoint",
            "sh",
            HELPER_IMAGE,
            "-c",
            script,
        ).stdout

    def remove_chain(self) -> None:
        self.iptables(
            f"while iptables -D DOCKER-USER -i {self.bridge} -o {self.bridge} -j {self.chain} 2>/dev/null; do :; done; "
            f"iptables -F {self.chain} 2>/dev/null || true; iptables -X {self.chain} 2>/dev/null || true"
        )


@pytest.fixture(scope="module")
def probe() -> Iterator[Probe]:
    tag = secrets.token_hex(3)
    p = Probe(
        tag=tag,
        chain=f"EMBRY0-ISO-PROBE-{tag.upper()}",
        restricted=f"isoprobe-{tag}-restricted",
        qa_net=f"isoprobe-{tag}-qa",
    )
    octet = secrets.randbelow(254) + 1
    _docker(
        "network",
        "create",
        "--driver",
        "bridge",
        "--opt",
        "com.docker.network.bridge.enable_ip_masquerade=false",
        # An explicit subnet only so a container can be given a chosen
        # address (--ip) below; the chain does not depend on it.
        "--subnet",
        f"10.213.{octet}.0/24",
        "--gateway",
        f"10.213.{octet}.1",
        p.restricted,
    )
    _docker("network", "create", p.qa_net)
    try:
        net = json.loads(_docker("network", "inspect", p.restricted).stdout)[0]
        p.bridge = bridge_interface(net)
        p.gateway = net["IPAM"]["Config"][0]["Gateway"]
        hardened = ("--cap-drop", "ALL", "--security-opt", "no-new-privileges")
        p.listen("git-proxy", _PROXY_PORT, "--network", p.restricted)
        p.listen("minio-proxy", _PROXY_PORT, "--network", p.restricted)
        p.listen("sandbox-a", _SANDBOX_PORT, "--network", p.restricted, *hardened)
        p.listen("sandbox-b", _SANDBOX_PORT, "--network", p.restricted, *hardened)
        p.listen("app-a", _APP_PORT, "--network", p.qa_net)
        _docker("network", "connect", p.qa_net, p.name("sandbox-a"))
        # A listener in the daemon's own namespace: what a dind_enabled
        # sandbox reaches through the bridge gateway.
        p.listen("gateway", _GATEWAY_PORT, "--network", "host")
        yield p
    finally:
        if p.bridge:
            p.remove_chain()
        for name in p.containers:
            _docker("rm", "-f", name, check=False)
        _docker("network", "rm", p.restricted, p.qa_net, check=False)


async def test_without_the_chain_sandboxes_reach_each_other(probe: Probe) -> None:
    """The gap #554 reports, and proof that ``reaches`` can see an open path."""
    probe.remove_chain()
    b = probe.ip(probe.name("sandbox-b"))
    assert probe.reaches(probe.name("sandbox-a"), b, _SANDBOX_PORT), (
        "sandbox-a could not reach sandbox-b even with no chain: the probe measures nothing"
    )


async def test_chain_blocks_sandbox_to_sandbox_and_keeps_the_proxies(probe: Probe) -> None:
    allowed = await probe.isolation().ensure(probe.proxies())
    a, b = probe.name("sandbox-a"), probe.name("sandbox-b")
    git, minio = probe.ip(probe.name("git-proxy")), probe.ip(probe.name("minio-proxy"))
    assert sorted(allowed) == sorted([git, minio])
    assert not probe.reaches(a, probe.ip(b), _SANDBOX_PORT), "sandbox-a reached sandbox-b through the chain"
    assert not probe.reaches(b, probe.ip(a), _SANDBOX_PORT), "sandbox-b reached sandbox-a through the chain"
    assert probe.reaches(a, git, _PROXY_PORT), "sandbox-a lost the git proxy"
    assert probe.reaches(a, minio, _PROXY_PORT), "sandbox-a lost the minio proxy"
    assert probe.reaches(b, git, _PROXY_PORT), "sandbox-b lost the git proxy"


async def test_chain_leaves_the_gateway_and_the_job_network_alone(probe: Probe) -> None:
    await probe.isolation().ensure(probe.proxies())
    a = probe.name("sandbox-a")
    assert probe.reaches(a, probe.gateway, _GATEWAY_PORT), "sandbox-a lost the daemon's namespace via the gateway"
    app = probe.ip(probe.name("app-a"), probe.qa_net)
    assert probe.reaches(a, app, _APP_PORT), "sandbox-a lost its own job network"


async def test_ensure_is_idempotent_and_puts_the_jump_first(probe: Probe) -> None:
    # Stands in for an egress rule inserted at the top after the last ensure.
    other = "-s 192.0.2.1/32 -j RETURN"
    probe.iptables(f"iptables -I DOCKER-USER 1 {other}")
    try:
        await probe.isolation().ensure(probe.proxies())
        await probe.isolation().ensure(probe.proxies())
        rules = [line for line in probe.iptables("iptables -S DOCKER-USER").splitlines() if line.startswith("-A ")]
    finally:
        probe.iptables(f"iptables -D DOCKER-USER {other}")
    jump = f"-A DOCKER-USER -i {probe.bridge} -o {probe.bridge} -j {probe.chain}"
    assert rules[0] == jump, f"the jump is not the first DOCKER-USER rule: {rules}"
    assert rules.count(jump) == 1, f"ensure duplicated the jump: {rules}"


async def test_rewrite_drops_a_former_proxy_address(probe: Probe) -> None:
    """A sandbox given a former proxy's address is not reachable once the chain is rewritten."""
    old = probe.listen("old-proxy", _PROXY_PORT, "--network", probe.restricted)
    await probe.isolation().ensure([*probe.proxies(), old])
    address = probe.ip(old)
    _docker("rm", "-f", old)
    probe.listen("sandbox-c", _SANDBOX_PORT, "--network", probe.restricted, "--ip", address)
    a = probe.name("sandbox-a")
    assert probe.reaches(a, address, _SANDBOX_PORT), "the stale chain should still let the old address through"
    await probe.isolation().ensure(probe.proxies())
    assert not probe.reaches(a, address, _SANDBOX_PORT), "sandbox-a reached a sandbox on a former proxy address"


async def test_refuses_to_write_a_chain_without_a_proxy(probe: Probe) -> None:
    await probe.isolation().ensure(probe.proxies())
    before = probe.iptables(f"iptables -S {probe.chain}")
    with pytest.raises(IsolationError, match="no proxy is running"):
        await probe.isolation().ensure([probe.name("no-such-proxy")])
    assert probe.iptables(f"iptables -S {probe.chain}") == before
