"""The proxy reachability integration tests touch a stack only when asked (#578).

They exec into an orchestrator container and start a sandbox in its DinD, so
running ``pytest tests/integration`` on a host with a production stack used to
reach into that stack whenever a container named ``embry0-orchestrator``
existed. This runs the module with a ``docker`` stub that answers every call
successfully, and checks that without the opt-in nothing is exec'd and the
skip names the variable that opts in.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_MODULE = _REPO / "tests" / "integration" / "test_proxy_reachability.py"
_OPT_IN = "EMBRY0_PROXY_PROBE_ORCHESTRATOR"


def test_without_the_opt_in_no_orchestrator_is_touched(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "docker.log"
    stub = bindir / "docker"
    stub.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\necho "code=200 code=401"\n')
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != _OPT_IN}
    env["PATH"] = f"{bindir}:{Path(sys.executable).parent}:/usr/bin:/bin"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(_MODULE), "-rs", "-q", "-p", "no:cacheprovider"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    execs = [line for line in (calls.read_text().splitlines() if calls.exists() else []) if line.startswith("exec")]
    assert execs == [], f"without {_OPT_IN} the tests exec'd into a container: {execs}"
    assert "2 skipped" in result.stdout, result.stdout + result.stderr
    assert _OPT_IN in result.stdout, f"the skip reason must name {_OPT_IN}: {result.stdout}"
