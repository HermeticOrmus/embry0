"""`embry0 start`, `build` and `build-sandbox` honour a COMPOSE_FILE overlay (#268).

infra/deploy.sh and the systemd unit run compose from infra/, so they read
COMPOSE_FILE from infra/.env; the CLI named infra/docker-compose.yml alone and
so always ran DinD. With the rootless overlay selected the CLI must compose
both files, build the sandbox and proxy images on the rootless sandbox daemon
(jobs run them from there, by local tag), not wait for a `dind` that the
overlay switched off, and never recreate `init-push-images`. Without
COMPOSE_FILE nothing changes. Nothing here touches Docker.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from embry0.cli import __main__ as cli

REPO_ROOT = Path(__file__).resolve().parents[3]
SOCKET = "unix:///run/embry0-sandbox/docker.sock"
_SERVICES = yaml.safe_load((REPO_ROOT / "infra" / "docker-compose.yml").read_text())["services"]
BUILDABLE = {name for name, svc in _SERVICES.items() if "build" in svc}
IMAGE_BUILDERS = {name for name in BUILDABLE if "images" in _SERVICES[name].get("profiles", [])}
STACK = BUILDABLE - IMAGE_BUILDERS


class Recorder:
    """Every command the CLI would run, with the DOCKER_HOST it would run it under."""

    def __init__(self, missing_images: bool = True) -> None:
        self.missing_images = missing_images
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        env = kw.get("env")
        self.calls.append((list(cmd), (env or {}).get("DOCKER_HOST")))
        failed = self.missing_images and cmd[:3] == ["docker", "image", "inspect"]
        return subprocess.CompletedProcess(cmd, 1 if failed else 0, stdout="", stderr="")

    def builds(self) -> list[tuple[list[str], str | None]]:
        return [(c, h) for c, h in self.calls if c[:2] == ["docker", "compose"] and "build" in c]


def _built(cmd: list[str]) -> set[str]:
    return {arg for arg in cmd[cmd.index("build") + 1 :] if not arg.startswith("-")}


def _files(cmd: list[str]) -> list[str]:
    return [Path(cmd[i + 1]).name for i, arg in enumerate(cmd) if arg == "-f"]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "infra").mkdir()
    for name in ("docker-compose.yml", "docker-compose.rootless.yml"):
        shutil.copy(REPO_ROOT / "infra" / name, tmp_path / "infra" / name)
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=x\n")
    monkeypatch.setenv("EMBRY0_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def rootless(home: Path) -> Path:
    (home / "infra" / ".env").write_text(
        "POSTGRES_PASSWORD=x\nCOMPOSE_FILE=docker-compose.yml:docker-compose.rootless.yml\nSANDBOX_DOCKER_GID=998\n"
    )
    return home


def _start(monkeypatch: pytest.MonkeyPatch, rec: Recorder) -> list[str]:
    waited: list[str] = []
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: rec(cmd, **kw))
    monkeypatch.setattr(cli.subprocess, "run", rec)
    monkeypatch.setattr(cli, "_docker_available", lambda: True)
    monkeypatch.setattr(cli, "_stack_running", lambda _home: False)
    monkeypatch.setattr(cli, "_wait_for_dind", lambda *a, **kw: waited.append("dind") or True)
    monkeypatch.setattr(
        cli, "_wait_for_sandbox_daemon", lambda host, *a, **kw: waited.append(host) or True, raising=False
    )
    cli.cmd_start(argparse.Namespace(port=None, host=None))
    return waited


def test_without_compose_file_the_cli_composes_the_base_file_alone(home):
    assert _files(cli._compose_cmd(home)) == ["docker-compose.yml"]


def test_the_cli_composes_every_file_infra_env_selects(rootless):
    assert _files(cli._compose_cmd(rootless)) == ["docker-compose.yml", "docker-compose.rootless.yml"], (
        "embry0 start ignores the COMPOSE_FILE in infra/.env that deploy.sh and the systemd unit honour"
    )


def test_start_builds_the_sandbox_images_on_the_rootless_daemon(rootless, monkeypatch):
    rec = Recorder(missing_images=True)
    waited = _start(monkeypatch, rec)
    on_sandbox_daemon = set().union(*(_built(c) for c, h in rec.builds() if h == SOCKET))
    on_host = set().union(*(_built(c) for c, h in rec.builds() if h is None))
    assert on_sandbox_daemon == IMAGE_BUILDERS, (
        f"sandbox and proxy images not built on the sandbox daemon: {rec.builds()}"
    )
    assert on_host == STACK, f"the orchestrator and frontend must still build on the host daemon: {rec.builds()}"
    assert waited == [SOCKET], f"start waits on {waited}; the overlay switches dind off"


def test_start_checks_the_sandbox_images_on_the_rootless_daemon(rootless, monkeypatch):
    rec = Recorder(missing_images=False)
    _start(monkeypatch, rec)
    inspects = [(c, h) for c, h in rec.calls if c[:3] == ["docker", "image", "inspect"]]
    on_sandbox_daemon = {image for c, h in inspects if h == SOCKET for image in c[3:]}
    assert "embry0-proxy:latest" in on_sandbox_daemon and "embry0-sandbox:latest" in on_sandbox_daemon, inspects
    assert rec.builds() == [], "images that all exist are rebuilt on every start"


def test_build_sandbox_under_rootless_rebuilds_there_and_recreates_no_init_push_images(rootless, monkeypatch, capsys):
    rec = Recorder(missing_images=False)
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: rec(cmd, **kw))
    cli.cmd_build_sandbox(argparse.Namespace())
    assert [(_built(c), h) for c, h in rec.builds()] == [(IMAGE_BUILDERS, SOCKET)], rec.builds()
    out = capsys.readouterr().out
    assert "--force-recreate orchestrator" in out and "init-push-images" not in out, out


def test_a_running_rootless_stack_counts_as_running_without_dind(rootless, monkeypatch):
    running = "embry0-frontend\nembry0-orchestrator\nembry0-postgres\n"
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=running, stderr=""))
    assert cli._stack_running(rootless), "a rootless stack has no embry0-dind and is never seen as running"


def test_the_dind_stack_still_needs_dind_to_count_as_running(home, monkeypatch):
    running = "embry0-frontend\nembry0-orchestrator\nembry0-postgres\n"
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=running, stderr=""))
    assert not cli._stack_running(home)


def test_a_missing_overlay_file_is_refused(home):
    (home / "infra" / ".env").write_text("COMPOSE_FILE=docker-compose.yml:docker-compose.nope.yml\n")
    with pytest.raises(SystemExit):
        cli._compose_cmd(home)


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _compose_available(), reason="needs the docker compose CLI")
def test_the_cli_command_parses_the_rootless_stack(rootless, monkeypatch, tmp_path):
    """The real parser, fed the CLI's own command: the overlay's SANDBOX_DOCKER_GID
    comes from infra/.env, and dind is off. Parse-only, aimed at no daemon."""
    (rootless / ".env").write_text("POSTGRES_PASSWORD=x\nMINIO_ROOT_USER=x\nMINIO_ROOT_PASSWORD=x\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    env["DOCKER_HOST"] = f"unix://{tmp_path}/no-daemon.sock"
    result = subprocess.run(
        [*cli._compose_cmd(rootless), "config", "--services"], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    services = set(result.stdout.split())
    assert "orchestrator" in services and "dind" not in services, services
