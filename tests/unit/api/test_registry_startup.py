"""#523 — the orchestrator logs in to the registry and pins its images at startup,
or refuses to start."""

import base64
import json
import os
from unittest.mock import patch

import pytest

from embry0.api.app import _configure_image_registry, _required_image_pins
from embry0.config import Embry0Config
from embry0.execution.image_registry import installed_image_pins, qualify_image

REQUIRED = sorted(_required_image_pins())


def _pin(i: int) -> str:
    return "sha256:" + f"{i:x}" * 64


def _config(**overrides) -> Embry0Config:
    with patch.dict(os.environ, {}, clear=True):
        return Embry0Config(_env_file=None, image_registry="registry:5000", **overrides)


def _auth_dir(tmp_path):
    d = tmp_path / "registry-auth"
    d.mkdir()
    (d / "username").write_text("embry0")
    (d / "password").write_text("p" * 40)
    return d


def _pins_file(tmp_path, images=REQUIRED):
    f = tmp_path / "pins"
    f.write_text("".join(f"{image} {_pin(i % 15 + 1)}\n" for i, image in enumerate(images)))
    return f


def test_the_required_pins_are_the_proxy_and_every_builtin_sandbox_image():
    assert REQUIRED == [
        "embry0-proxy:latest",
        "embry0-sandbox-dev-jvm-python:latest",
        "embry0-sandbox-dev-python:latest",
        "embry0-sandbox-qa:latest",
        "embry0-sandbox:latest",
    ]


def test_writes_the_login_and_installs_the_pins(tmp_path):
    docker_dir = tmp_path / "docker"
    config = _config(registry_auth_dir=str(_auth_dir(tmp_path)), image_pins_file=str(_pins_file(tmp_path)))

    _configure_image_registry(config, {"DOCKER_CONFIG": str(docker_dir)})

    auth = json.loads((docker_dir / "config.json").read_text())["auths"]["registry:5000"]["auth"]
    assert base64.b64decode(auth).decode() == "embry0:" + "p" * 40
    assert sorted(installed_image_pins()) == REQUIRED
    assert qualify_image("embry0-proxy:latest", "registry:5000").startswith(
        "registry:5000/embry0-proxy:latest@sha256:"
    ), "after startup the proxy must launch by digest"


def test_a_missing_login_refuses_the_start(tmp_path):
    config = _config(registry_auth_dir=str(tmp_path / "absent"))
    with pytest.raises(RuntimeError, match="Refusing to start: cannot read the registry username"):
        _configure_image_registry(config, {"DOCKER_CONFIG": str(tmp_path / "docker")})


def test_a_missing_pins_file_refuses_the_start(tmp_path):
    config = _config(image_pins_file=str(tmp_path / "absent"))
    with pytest.raises(RuntimeError, match="Refusing to start: cannot read the image pins file"):
        _configure_image_registry(config, {})
    assert installed_image_pins() == {}


def test_a_malformed_pins_file_refuses_the_start(tmp_path):
    bad = tmp_path / "pins"
    bad.write_text("embry0-proxy:latest latest\n")
    with pytest.raises(RuntimeError, match="Refusing to start: line 1"):
        _configure_image_registry(_config(image_pins_file=str(bad)), {})


def test_a_pins_file_without_the_proxy_refuses_the_start(tmp_path):
    """The proxy holds the forge and admin tokens: it must never run by tag."""
    pins = _pins_file(tmp_path, images=[i for i in REQUIRED if i != "embry0-proxy:latest"])
    with pytest.raises(RuntimeError, match="has no pin for embry0-proxy:latest"):
        _configure_image_registry(_config(image_pins_file=str(pins)), {})
    assert installed_image_pins() == {}, "a refused start must not leave half the pins installed"


def test_a_pins_file_without_a_sandbox_image_refuses_the_start(tmp_path):
    pins = _pins_file(tmp_path, images=[i for i in REQUIRED if i != "embry0-sandbox-qa:latest"])
    with pytest.raises(RuntimeError, match="has no pin for embry0-sandbox-qa:latest"):
        _configure_image_registry(_config(image_pins_file=str(pins)), {})


@pytest.mark.parametrize("field", ["registry_auth_dir", "image_pins_file"])
def test_either_setting_without_a_registry_refuses_the_start(tmp_path, field):
    with patch.dict(os.environ, {}, clear=True):
        config = Embry0Config(_env_file=None, image_registry="", **{field: str(tmp_path)})
    with pytest.raises(RuntimeError, match="IMAGE_REGISTRY is empty"):
        _configure_image_registry(config, {})


def test_nothing_configured_changes_nothing(tmp_path):
    """Tests, dev layouts and registries that need no login keep today's behaviour."""
    with patch.dict(os.environ, {}, clear=True):
        config = Embry0Config(_env_file=None)
    _configure_image_registry(config, {"DOCKER_CONFIG": str(tmp_path / "docker")})
    assert installed_image_pins() == {}
    assert not (tmp_path / "docker").exists()


def test_a_registry_without_pins_is_warned_about(tmp_path):
    import structlog

    with structlog.testing.capture_logs() as logs:
        _configure_image_registry(_config(), {})
    assert any(e["event"] == "image_pins_not_configured" for e in logs), logs
