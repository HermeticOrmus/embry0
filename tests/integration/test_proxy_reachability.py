"""Integration: a sandbox on sandbox-restricted can reach the git-proxy by DNS.

Runs against a real stack, not a unit-mocked DinD: it execs into the stack's
orchestrator container and starts an unenrolled sandbox container in its
DinD, on its ``sandbox-restricted`` network. Opt-in (#578). Name the
orchestrator container of a stack you may do that to::

    EMBRY0_PROXY_PROBE_ORCHESTRATOR=embry0-orchestrator \\
        pytest tests/integration/test_proxy_reachability.py

Unset, both tests skip, whatever containers the local daemon runs. Skips too
if that orchestrator cannot talk to its DinD.
"""

import os
import subprocess

import pytest

ORCHESTRATOR = os.environ.get("EMBRY0_PROXY_PROBE_ORCHESTRATOR", "")

pytestmark = [
    pytest.mark.requires_docker,
    pytest.mark.skipif(
        not ORCHESTRATOR,
        reason="set EMBRY0_PROXY_PROBE_ORCHESTRATOR to the orchestrator container of a stack this may start a sandbox in",
    ),
]


def _run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout + p.stderr


@pytest.fixture
def has_dind() -> bool:
    rc, _ = _run(
        [
            "docker",
            "exec",
            ORCHESTRATOR,
            "docker",
            "--host",
            "tcp://dind:2376",
            "--tlsverify",
            "--tlscacert=/certs/client/ca.pem",
            "--tlscert=/certs/client/cert.pem",
            "--tlskey=/certs/client/key.pem",
            "version",
        ]
    )
    return rc == 0


def test_git_proxy_reachable_from_sandbox_restricted(has_dind):
    if not has_dind:
        pytest.skip("DinD not reachable — start the local stack first")

    rc, out = _run(
        [
            "docker",
            "exec",
            ORCHESTRATOR,
            "docker",
            "--host",
            "tcp://dind:2376",
            "--tlsverify",
            "--tlscacert=/certs/client/ca.pem",
            "--tlscert=/certs/client/cert.pem",
            "--tlskey=/certs/client/key.pem",
            "run",
            "--rm",
            "--network=sandbox-restricted",
            "embry0-sandbox:latest",
            "bash",
            "-lc",
            "curl -sf http://git-proxy:9101/health -o /dev/null -w 'code=%{http_code}\\n'",
        ]
    )

    assert rc == 0, f"sandbox->git-proxy curl failed: {out}"
    assert "code=200" in out, f"unexpected response: {out}"


def test_git_proxy_hands_no_credential_to_a_sandbox(has_dind):
    """#521: the retired credential-helper endpoint never answers with a credential."""
    if not has_dind:
        pytest.skip("DinD not reachable")

    rc, out = _run(
        [
            "docker",
            "exec",
            ORCHESTRATOR,
            "docker",
            "--host",
            "tcp://dind:2376",
            "--tlsverify",
            "--tlscacert=/certs/client/ca.pem",
            "--tlscert=/certs/client/cert.pem",
            "--tlskey=/certs/client/key.pem",
            "run",
            "--rm",
            "--network=sandbox-restricted",
            "embry0-sandbox:latest",
            "bash",
            "-lc",
            "curl -s http://git-proxy:9101/git-credentials -w '\\ncode=%{http_code}\\n'",
        ]
    )

    assert rc == 0, f"sandbox->git-proxy curl failed: {out}"
    assert "code=401" in out, f"an unenrolled caller must be refused: {out}"
    assert "password=" not in out
