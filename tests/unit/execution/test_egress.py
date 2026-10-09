"""EMB-29 egress-allowlist enforcement unit tests.

Covers allowlist derivation/resolution, the iptables script shapes shipped
to the DinD-netns helper, and the SandboxManager fail-closed/teardown
wiring. The DockerClient is mocked — rule semantics are asserted on the
generated commands (mirroring test_network_assertion.py's approach).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.execution.runtime.egress import (
    BLOCKED_PRIVATE_RANGES,
    LOG_PREFIX,
    EgressEnforcer,
    derive_deployed_egress_allowlist,
    resolve_allowlist_to_ips,
)

# ---------------------------------------------------------------------------
# allowlist derivation + resolution
# ---------------------------------------------------------------------------


def test_derive_declares_lan_targets_only():
    """Only PRIVATE destinations need declaring — public egress stays open."""
    out = derive_deployed_egress_allowlist(
        frontend_url="https://app.qa.test/",
        extra_hosts={
            "app.qa.test": "192.168.1.10",
            "hub.qa.test": "192.168.1.10",
        },
    )
    assert out == ["192.168.1.10"]  # deduped, no public hosts


def test_derive_public_frontend_yields_no_declarations():
    """A CDN-fronted public frontend needs nothing declared (its rotating
    public IPs are unaffected by the private-range blocklist)."""
    out = derive_deployed_egress_allowlist(
        frontend_url="https://definitely-not-a-real-host.invalid/login",
        extra_hosts={},
    )
    assert out == []


def test_derive_private_ip_frontend_is_declared():
    out = derive_deployed_egress_allowlist(
        frontend_url="http://192.168.1.10/",
        extra_hosts={},
    )
    assert out == ["192.168.1.10"]


def test_resolve_passes_ips_and_cidrs_through():
    assert resolve_allowlist_to_ips(["192.168.1.10", "10.0.0.0/24", ""]) == [
        "192.168.1.10",
        "10.0.0.0/24",
    ]


def test_resolve_skips_unresolvable_hostnames():
    out = resolve_allowlist_to_ips(["definitely-not-a-real-host.invalid", "127.0.0.1"])
    assert out == ["127.0.0.1"]


# ---------------------------------------------------------------------------
# EgressEnforcer command shapes
# ---------------------------------------------------------------------------


def _docker() -> MagicMock:
    docker = MagicMock()
    docker._build_base_cmd = MagicMock(return_value=["docker", "--host", "tcp://dind:2376"])
    docker.run_cmd = AsyncMock(return_value="")
    return docker


def _helper_script(docker: MagicMock, call_idx: int = 0) -> str:
    cmd = docker.run_cmd.await_args_list[call_idx].args[0]
    assert cmd[:3] == ["docker", "--host", "tcp://dind:2376"]
    assert "--network" in cmd and "host" in cmd
    assert "--cap-add" in cmd and "NET_ADMIN" in cmd
    return str(cmd[-1])


@pytest.mark.asyncio
async def test_apply_builds_private_range_blocklist():
    docker = _docker()
    enforcer = EgressEnforcer(docker)
    await enforcer.apply("172.20.0.5", ["192.168.1.10", "10.1.2.3"])

    script = _helper_script(docker)
    # Every rule is keyed on the sandbox source IP.
    assert "-s 172.20.0.5" in script
    # Declared LAN exceptions.
    assert "-d 192.168.1.10 -j RETURN" in script
    assert "-d 10.1.2.3 -j RETURN" in script
    # Every private range gets LOG + DROP; nothing is a blanket DROP.
    for blocked in BLOCKED_PRIVATE_RANGES:
        assert f"-d {blocked} -j DROP" in script
        assert f"-d {blocked} " in script
    assert "-s 172.20.0.5 -j DROP" not in script  # public egress stays open
    assert LOG_PREFIX in script
    assert "--ctstate ESTABLISHED,RELATED" in script
    # DROPs inserted first (end up below the allows after position-1 inserts).
    assert script.index("-j DROP") < script.index("LOG")
    assert script.index("LOG") < script.index("-j RETURN")


@pytest.mark.asyncio
async def test_apply_rejects_garbage_source_ip():
    enforcer = EgressEnforcer(_docker())
    with pytest.raises(ValueError):
        await enforcer.apply("not-an-ip; rm -rf /", ["10.0.0.1"])


@pytest.mark.asyncio
async def test_clear_deletes_by_line_number():
    docker = _docker()
    enforcer = EgressEnforcer(docker)
    await enforcer.clear("172.20.0.5")
    script = _helper_script(docker)
    assert "iptables -S DOCKER-USER" in script
    assert "172.20.0.5/32" in script
    assert "iptables -D DOCKER-USER" in script


# A stand-in for iptables that keeps DOCKER-USER in a file and prints it
# the way iptables does for -S and for -L --line-numbers -n, so the helper
# scripts run for real instead of being matched as strings (#566).
_FAKE_IPTABLES = r"""
import os, shlex, sys

state = os.environ["FAKE_IPTABLES_STATE"]
rules = open(state).read().splitlines() if os.path.exists(state) else []
args = sys.argv[1:]
if args[:1] == ["-I"]:
    spec = args[3:]
    for i, word in enumerate(spec[:-1]):  # iptables stores a bare address as /32
        if word in ("-s", "-d") and "/" not in spec[i + 1]:
            spec[i + 1] += "/32"
    rules.insert(int(args[2]) - 1, "-A DOCKER-USER " + shlex.join(spec))
elif args[:1] == ["-D"]:
    del rules[int(args[2]) - 1]
elif args[:1] == ["-S"]:
    for line in ["-N DOCKER-USER", *rules]:
        print(line)
elif args[:1] == ["-L"]:
    print("Chain DOCKER-USER (1 references)")
    print("num  target     prot opt source               destination")
    for num, rule in enumerate(rules, 1):
        f = shlex.split(rule)
        get = lambda flag: f[f.index(flag) + 1] if flag in f else "0.0.0.0/0"
        src, dst = (get(x).removesuffix("/32") for x in ("-s", "-d"))
        print(f"{num:<4} {get('-j'):<10} all  --  {src:<20} {dst}")
else:
    sys.exit(f"fake iptables: unsupported {args}")
with open(state, "w") as out:
    out.writelines(rule + "\n" for rule in rules)
"""


def _chain_docker(tmp_path) -> tuple[MagicMock, Callable[[], list[str]]]:
    """A DockerClient mock that runs each helper script against the fake chain.

    Returns the mock and a reader for the chain's rules in ``-S`` form.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "iptables"
    fake.write_text(f"#!{sys.executable}\n{_FAKE_IPTABLES}")
    fake.chmod(0o755)
    state = tmp_path / "docker-user.rules"
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_IPTABLES_STATE": str(state)}

    async def run_cmd(cmd: list[str], timeout: int = 30) -> str:
        return subprocess.run(["sh", "-c", cmd[-1]], env=env, check=True, capture_output=True, text=True).stdout

    docker = MagicMock()
    docker._build_base_cmd = MagicMock(side_effect=lambda: ["docker", "--host", "tcp://dind:2376"])
    docker.run_cmd = AsyncMock(side_effect=run_cmd)
    return docker, lambda: state.read_text().splitlines() if state.exists() else []


@pytest.mark.asyncio
async def test_clear_leaves_rules_of_an_address_it_prefixes(tmp_path):
    """Clearing x.y.z.8 must not touch the rules of x.y.z.80 (#566)."""
    docker, rules = _chain_docker(tmp_path)
    enforcer = EgressEnforcer(docker)
    await enforcer.apply("10.77.0.8", [])
    await enforcer.apply("10.77.0.80", [])
    # A third sandbox whose DESTINATION allow contains the cleared address.
    await enforcer.apply("10.77.0.9", ["10.77.0.8"])
    per_sandbox = 2 * len(BLOCKED_PRIVATE_RANGES) + 1
    assert sum("-s 10.77.0.8/32 " in r for r in rules()) == per_sandbox, rules()

    await enforcer.clear("10.77.0.8")
    after = rules()

    assert not any("-s 10.77.0.8/32 " in r for r in after), f"clear left rules of 10.77.0.8: {after}"
    kept_80 = sum("-s 10.77.0.80/32 " in r for r in after)
    assert kept_80 == per_sandbox, f"clear(10.77.0.8) removed rules of 10.77.0.80: {kept_80} of {per_sandbox} left"
    kept_9 = sum("-s 10.77.0.9/32 " in r for r in after)
    assert kept_9 == per_sandbox + 1, (
        f"clear(10.77.0.8) removed rules of 10.77.0.9, whose allow targets 10.77.0.8: {kept_9} of {per_sandbox + 1}"
    )


@pytest.mark.asyncio
async def test_clear_on_an_empty_chain_is_a_no_op(tmp_path):
    docker, rules = _chain_docker(tmp_path)
    await EgressEnforcer(docker).clear("10.77.0.8")
    assert rules() == []


@pytest.mark.asyncio
async def test_container_ip_reads_network_specific_address():
    docker = _docker()
    docker.run_cmd = AsyncMock(return_value="172.20.0.5\n")
    enforcer = EgressEnforcer(docker)
    ip = await enforcer.container_ip("cid123", "sandbox-internet")
    assert ip == "172.20.0.5"
    cmd = docker.run_cmd.await_args.args[0]
    assert "inspect" in cmd
    assert any("sandbox-internet" in part for part in cmd)


@pytest.mark.asyncio
async def test_container_ip_none_when_not_attached():
    docker = _docker()
    docker.run_cmd = AsyncMock(return_value="\n")
    assert await EgressEnforcer(docker).container_ip("cid123") is None


@pytest.mark.asyncio
async def test_verify_ready_warns_and_returns_false_on_failure():
    docker = _docker()
    docker.run_cmd = AsyncMock(side_effect=RuntimeError("no dind"))
    assert await EgressEnforcer(docker).verify_ready() is False


@pytest.mark.asyncio
async def test_ensure_helper_image_skips_build_when_present():
    docker = _docker()
    enforcer = EgressEnforcer(docker)
    await enforcer.ensure_helper_image()
    # Single inspect call, no build.
    assert docker.run_cmd.await_count == 1
    assert "image" in docker.run_cmd.await_args.args[0]
