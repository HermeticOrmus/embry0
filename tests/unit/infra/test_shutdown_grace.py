"""Guard: the orchestrator gets the time its shutdown drain needs (#307).

On SIGTERM uvicorn first closes its connections (``--timeout-graceful-shutdown``),
then the lifespan drains the running jobs for ``SHUTDOWN_DRAIN_SECONDS``, then
the rest of shutdown runs. Compose SIGKILLs the container after its
``stop_grace_period`` — 10 s unless set — and a killed orchestrator stamps no
job as interrupted. systemd's ``docker compose down`` must outlast that too.
"""

import re
from pathlib import Path

import yaml

from embry0.config import Embry0Config

REPO_ROOT = Path(__file__).resolve().parents[3]
# The bounded steps after the drain: the cancelled-task wait (30 s in the
# lifespan), the drain's own cancel wait (10 s), proxies and the DB pool.
REST_OF_SHUTDOWN_SECONDS = 45


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", value)
    assert match and value, f"unreadable duration {value!r}"
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def _uvicorn_graceful_seconds() -> int:
    dockerfile = (REPO_ROOT / "infra" / "Dockerfile.orchestrator").read_text()
    match = re.search(r'"--timeout-graceful-shutdown",\s*"(\d+)"', dockerfile)
    assert match, "Dockerfile.orchestrator no longer sets --timeout-graceful-shutdown; re-derive this budget"
    return int(match.group(1))


def _grace_period() -> int:
    services = yaml.safe_load((REPO_ROOT / "infra" / "docker-compose.yml").read_text())["services"]
    raw = services["orchestrator"].get("stop_grace_period")
    assert raw, "the orchestrator has no stop_grace_period: compose would SIGKILL it after 10 s, mid-drain"
    return _seconds(str(raw))


def test_the_orchestrator_stop_grace_period_covers_the_drain() -> None:
    needed = (
        _uvicorn_graceful_seconds() + Embry0Config(_env_file=None).shutdown_drain_seconds + REST_OF_SHUTDOWN_SECONDS
    )
    assert _grace_period() >= needed, (
        f"stop_grace_period {_grace_period()} s is shorter than uvicorn's graceful shutdown + the default "
        f"SHUTDOWN_DRAIN_SECONDS + the rest of shutdown ({needed} s): the orchestrator would be killed mid-drain"
    )


def test_the_rootless_overlay_keeps_the_grace_period() -> None:
    overlay = (REPO_ROOT / "infra" / "docker-compose.rootless.yml").read_text()
    assert "stop_grace_period" not in overlay, (
        "the rootless overlay must inherit the base file's stop_grace_period, not set its own"
    )


def test_systemd_waits_for_compose_down() -> None:
    unit = (REPO_ROOT / "infra" / "systemd" / "embry0.service").read_text()
    match = re.search(r"^TimeoutStopSec=(\d+)$", unit, re.MULTILINE)
    assert match, "embry0.service has no TimeoutStopSec: systemd's 90 s default ends `docker compose down` mid-drain"
    assert int(match.group(1)) > _grace_period(), (
        f"TimeoutStopSec={match.group(1)} must exceed the orchestrator's stop_grace_period ({_grace_period()} s)"
    )
