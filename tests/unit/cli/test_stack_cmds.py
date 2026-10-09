"""`embry0 start`, `build` and `build-sandbox` build what the stack needs (#531).

init-push-images exits non-zero unless every sandbox and proxy image is on the
host daemon, and the orchestrator waits for it. A start that built only the
orchestrator and frontend therefore never started the orchestrator. Nothing
here touches Docker: every command the CLI would run is recorded instead.
"""

import argparse
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from embry0.cli import __main__ as cli

REPO_ROOT = Path(__file__).resolve().parents[3]
_SERVICES = yaml.safe_load((REPO_ROOT / "infra" / "docker-compose.yml").read_text())["services"]
# Read from the compose file, not from the CLI's own lists, so a list that
# drops a service cannot also shrink what the test expects.
BUILDABLE = {name for name, svc in _SERVICES.items() if "build" in svc}
IMAGE_BUILDERS = {name for name in BUILDABLE if "images" in _SERVICES[name].get("profiles", [])}

# The image each builder service produces; any one missing breaks the push.
SANDBOX_AND_PROXY_IMAGES = {
    "embry0-proxy:latest",
    "embry0-sandbox:latest",
    "embry0-sandbox-dev-python:latest",
    "embry0-sandbox-dev-jvm-python:latest",
    "embry0-sandbox-qa:latest",
}


class Recorder:
    """Stands in for every docker invocation. `missing_images` makes
    `docker image inspect` fail, as it does when any named image is absent."""

    def __init__(self, missing_images: bool) -> None:
        self.missing_images = missing_images
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        failed = self.missing_images and cmd[:3] == ["docker", "image", "inspect"]
        return subprocess.CompletedProcess(cmd, 1 if failed else 0, stdout="", stderr="")

    def builds(self) -> list[list[str]]:
        return [c for c in self.calls if "build" in c and c[:2] == ["docker", "compose"]]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "infra").mkdir()
    shutil.copy(REPO_ROOT / "infra" / "docker-compose.yml", tmp_path / "infra" / "docker-compose.yml")
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=x\n")
    monkeypatch.setenv("EMBRY0_HOME", str(tmp_path))
    return tmp_path


def _start(home: Path, monkeypatch: pytest.MonkeyPatch, *, missing_images: bool) -> Recorder:
    rec = Recorder(missing_images)
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: rec(cmd, **kw))
    monkeypatch.setattr(cli.subprocess, "run", rec)
    monkeypatch.setattr(cli, "_docker_available", lambda: True)
    monkeypatch.setattr(cli, "_stack_running", lambda _home: False)
    monkeypatch.setattr(cli, "_wait_for_dind", lambda: True)
    cli.cmd_start(argparse.Namespace(port=None, host=None))
    return rec


def _built_services(build: list[str]) -> set[str]:
    """What a `docker compose … build …` command builds, as compose decides it:
    the services named, or with none named, every buildable service whose
    profile is enabled (a service without profiles always is)."""
    named = {arg for arg in build[build.index("build") + 1 :] if not arg.startswith("-")}
    if named:
        return named
    enabled = {build[i + 1] for i, arg in enumerate(build) if arg == "--profile"}
    return {name for name in BUILDABLE if set(_SERVICES[name].get("profiles", [])) <= enabled}


def test_start_on_a_fresh_host_builds_the_sandbox_and_proxy_images(home, monkeypatch):
    rec = _start(home, monkeypatch, missing_images=True)
    builds = rec.builds()
    assert len(builds) == 1, f"expected one compose build, got {builds}"
    built = _built_services(builds[0])
    missing = IMAGE_BUILDERS - built
    assert not missing, f"embry0 start does not build {sorted(missing)}, so init-push-images fails"
    assert built == BUILDABLE, f"embry0 start builds {sorted(built)}, compose can build {sorted(BUILDABLE)}"


def test_start_checks_every_built_image_not_just_the_stack(home, monkeypatch):
    rec = _start(home, monkeypatch, missing_images=False)
    inspects = [c for c in rec.calls if c[:3] == ["docker", "image", "inspect"]]
    assert inspects, f"embry0 start never checks the images on the host: {rec.calls}"
    checked = set(inspects[0][3:])
    missing = SANDBOX_AND_PROXY_IMAGES - checked
    assert not missing, f"a host without {sorted(missing)} counts as built, and is never rebuilt"
    assert {"embry0-orchestrator", "embry0-frontend"} <= checked, checked
    assert rec.builds() == [], "images that all exist are rebuilt on every start"


def test_start_builds_nothing_inside_dind(home, monkeypatch):
    rec = _start(home, monkeypatch, missing_images=True)
    stray = [c for c in rec.calls if c[:2] == ["docker", "cp"] or (c[:2] == ["docker", "exec"] and "build" in c)]
    assert not stray, (
        f"embry0 start builds inside DinD: {stray}. Jobs run the images init-push-images pinned, "
        f"so an image built there is never used."
    )


def test_build_builds_every_service(home, monkeypatch):
    rec = Recorder(missing_images=False)
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: rec(cmd, **kw))
    cli.cmd_build(argparse.Namespace(cached=True))
    (build,) = rec.builds()
    assert _built_services(build) == BUILDABLE, f"embry0 build skips {sorted(BUILDABLE - _built_services(build))}"


def test_build_sandbox_rebuilds_on_the_host_and_restarts_nothing(home, monkeypatch, capsys):
    rec = Recorder(missing_images=False)
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: rec(cmd, **kw))
    cli.cmd_build_sandbox(argparse.Namespace())
    builds = rec.builds()
    assert len(builds) == 1 and _built_services(builds[0]) == IMAGE_BUILDERS, (
        f"embry0 build-sandbox does not build the image builders on the host: {rec.calls}"
    )
    assert not [c for c in rec.calls if "up" in c or c[:2] == ["docker", "exec"]], rec.calls
    assert "--force-recreate init-push-images orchestrator" in capsys.readouterr().out
