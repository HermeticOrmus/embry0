"""infra/deploy.sh leaves a deploy marker that DeployLedgerSync can read.

The script and the parser are the two ends of one contract: a self case only
walks past `merged` if the line the deploy pushes is the line the watcher
parses. Neither end can see the other in its own tests, so this pins them.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from embry0.services.healing.deploys import parse_deploy_marker

_SCRIPT = Path(__file__).resolve().parents[3] / "infra" / "deploy.sh"


def _marker(*args: str) -> str:
    out = subprocess.run(
        ["bash", str(_SCRIPT), "marker", *args],
        env={"PATH": "/usr/bin:/bin", "EMBRY0_DEPLOY_DRY_RUN": "1"},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return out.stdout.strip()


def test_the_deploy_marker_is_the_line_the_ledger_sync_parses() -> None:
    line = _marker("65340420f0a4", "orchestrator", "frontend")
    marker = parse_deploy_marker(line, datetime(2026, 9, 27, tzinfo=UTC))
    assert marker is not None, f"DeployLedgerSync cannot read {line!r}"
    assert marker.sha == "65340420f0a4"
    assert marker.services == ("orchestrator", "frontend")


def test_a_single_service_deploy_is_a_marker_too() -> None:
    marker = parse_deploy_marker(_marker("da929bdd0000", "orchestrator"), datetime(2026, 9, 27, tzinfo=UTC))
    assert marker is not None and marker.services == ("orchestrator",)


def test_marker_needs_a_sha_and_a_service() -> None:
    result = subprocess.run(
        ["bash", str(_SCRIPT), "marker", "abc1234"],
        env={"PATH": "/usr/bin:/bin", "EMBRY0_DEPLOY_DRY_RUN": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2 and "usage" in result.stderr
