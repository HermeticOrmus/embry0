"""#523 — the orchestrator's docker CLI logs in to the image registry."""

import base64
import json
import stat

import pytest

from embry0.execution.registry_auth import (
    RegistryAuthError,
    docker_config_dir,
    read_registry_credential,
    registry_host,
    write_docker_login,
)


def _auth_dir(tmp_path, username="embry0", password="p" * 40):
    d = tmp_path / "registry-auth"
    d.mkdir()
    if username is not None:
        (d / "username").write_text(username)
    if password is not None:
        (d / "password").write_text(password)
    return d


def test_reads_the_generated_credential(tmp_path):
    assert read_registry_credential(_auth_dir(tmp_path, password="s3cret\n")) == ("embry0", "s3cret")


@pytest.mark.parametrize(
    ("username", "password", "missing"),
    [(None, "pw", "username"), ("embry0", None, "password"), ("embry0", "  \n", "password"), ("", "pw", "username")],
)
def test_a_missing_or_empty_file_is_refused_by_name(tmp_path, username, password, missing):
    with pytest.raises(RegistryAuthError, match=f"registry {missing}"):
        read_registry_credential(_auth_dir(tmp_path, username, password))


def test_a_username_with_a_colon_is_refused(tmp_path):
    """Basic auth joins the two with ':'; one in the username would split wrongly."""
    with pytest.raises(RegistryAuthError, match="contains ':'"):
        read_registry_credential(_auth_dir(tmp_path, username="a:b"))


@pytest.mark.parametrize(
    ("registry", "host"),
    [("registry:5000", "registry:5000"), ("registry:5000/", "registry:5000"), ("ghcr.io/acme", "ghcr.io")],
)
def test_the_login_is_keyed_by_the_registry_host(registry, host):
    assert registry_host(registry) == host


def test_docker_config_dir_follows_docker_config_then_home(tmp_path, monkeypatch):
    assert docker_config_dir({"DOCKER_CONFIG": str(tmp_path)}) == tmp_path
    monkeypatch.setenv("HOME", str(tmp_path))
    assert docker_config_dir({}) == tmp_path / ".docker"


def test_writes_a_login_the_docker_cli_reads(tmp_path):
    path = write_docker_login(tmp_path / "docker", "registry:5000", "embry0", "s3cret")

    config = json.loads(path.read_text())
    token = config["auths"]["registry:5000"]["auth"]
    assert base64.b64decode(token).decode() == "embry0:s3cret", "docker reads base64('<user>:<password>')"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, "the login file must be private"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_keeps_other_entries_in_an_existing_config(tmp_path):
    directory = tmp_path / "docker"
    directory.mkdir()
    (directory / "config.json").write_text(
        json.dumps({"auths": {"ghcr.io": {"auth": "eA=="}}, "credsStore": "", "detachKeys": "ctrl-x"})
    )

    write_docker_login(directory, "registry:5000", "embry0", "new")
    write_docker_login(directory, "registry:5000", "embry0", "newer")

    config = json.loads((directory / "config.json").read_text())
    assert config["auths"]["ghcr.io"] == {"auth": "eA=="}
    assert config["detachKeys"] == "ctrl-x"
    assert base64.b64decode(config["auths"]["registry:5000"]["auth"]).decode() == "embry0:newer"
    assert [p.name for p in directory.iterdir()] == ["config.json"], "no temp file may be left behind"


def test_an_unreadable_config_is_refused_not_overwritten(tmp_path):
    directory = tmp_path / "docker"
    directory.mkdir()
    (directory / "config.json").write_text("[]")
    with pytest.raises(RegistryAuthError, match="not a JSON object"):
        write_docker_login(directory, "registry:5000", "embry0", "pw")
    assert (directory / "config.json").read_text() == "[]"
