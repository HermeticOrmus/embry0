"""#523 — the in-stack registry refuses anonymous clients, and the images the
bootstrap pushes run by the digest it recorded.

Three parts are wired together here, and each test names the one it guards:
the compose services and volumes, `registry-auth-init.sh` (generates the
login) and `push-images-to-registry.sh` (logs in, pushes, records the pins).
The scripts run for real against stub `docker` / `htpasswd` binaries.
"""

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "infra" / "docker-compose.yml"
PUSH_SCRIPT = REPO_ROOT / "infra" / "scripts" / "push-images-to-registry.sh"
AUTH_INIT_SCRIPT = REPO_ROOT / "infra" / "scripts" / "registry-auth-init.sh"


@pytest.fixture(scope="module")
def services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _volume_mounts(service: dict, volume: str) -> list[tuple[str, bool]]:
    """``(target, read_only)`` for each mount of the named volume ``volume``."""
    out = []
    for entry in service.get("volumes", []):
        source, target, *mode = entry.split(":")
        if source == volume:
            out.append((target, mode == ["ro"]))
    return out


def _mounters(services: dict, volume: str) -> dict[str, list[tuple[str, bool]]]:
    return {name: m for name, svc in services.items() if (m := _volume_mounts(svc, volume))}


# ── compose ─────────────────────────────────────────────────────────────────


def test_the_registry_requires_htpasswd_auth(services):
    registry = services["registry"]
    env = registry.get("environment", {})
    assert env.get("REGISTRY_AUTH") == "htpasswd", "the registry must refuse anonymous clients"
    assert env.get("REGISTRY_AUTH_HTPASSWD_PATH") == "/auth/htpasswd"
    assert ("/auth", True) in _volume_mounts(registry, "registry-auth")
    assert registry["depends_on"]["registry-auth-init"]["condition"] == "service_completed_successfully"


def test_the_registry_is_healthy_only_while_it_refuses_anonymous_clients(services):
    """Health gates init-push-images and so the orchestrator: an open registry
    (200 on /v2/) must not count as healthy."""
    test = " ".join(services["registry"]["healthcheck"]["test"])
    assert "401" in test, f"the registry healthcheck must require a 401 from /v2/, got {test!r}"


def test_the_login_is_generated_offline_by_a_pinned_image(services):
    init = services["registry-auth-init"]
    assert init.get("network_mode") == "none"
    assert re.search(r"@sha256:[0-9a-f]{64}$", init["image"]), "pin the htpasswd image by digest"
    assert init["command"] == ["/scripts/registry-auth-init.sh"]
    assert _volume_mounts(init, "registry-auth") == [("/auth", False)]
    assert init.get("restart") == "no"


def test_only_the_registry_and_its_clients_mount_the_login(services):
    """The volume is the boundary around the password (#523): the internet-facing
    services (mcp, cloudflared) and the sandbox daemon must not see it."""
    assert _mounters(services, "registry-auth") == {
        "registry-auth-init": [("/auth", False)],
        "registry": [("/auth", True)],
        "init-push-images": [("/auth", True)],
        "orchestrator": [("/run/embry0/registry-auth", True)],
    }


def test_only_the_pusher_can_write_the_pins(services):
    assert _mounters(services, "image-pins") == {
        "init-push-images": [("/pins", False)],
        "orchestrator": [("/run/embry0/image-pins", True)],
    }


def test_the_pusher_logs_in_and_records_pins_where_they_are_mounted(services):
    env = services["init-push-images"]["environment"]
    assert env["REGISTRY_AUTH_DIR"] == "/auth"
    assert env["PINS_FILE"].startswith("/pins/"), env


def test_the_orchestrator_reads_the_login_and_pins_where_they_are_mounted(services):
    orch = services["orchestrator"]
    env = orch["environment"]
    assert env["REGISTRY_AUTH_DIR"] == "/run/embry0/registry-auth"
    assert env["IMAGE_PINS_FILE"] == "/run/embry0/image-pins/pins"
    pins_name = Path(services["init-push-images"]["environment"]["PINS_FILE"]).name
    assert Path(env["IMAGE_PINS_FILE"]).name == pins_name, "the orchestrator must read the file the pusher writes"
    assert orch["depends_on"]["init-push-images"]["condition"] == "service_completed_successfully"


def test_the_sandbox_daemon_keeps_its_registry_config(services):
    """Logins are client-side; DinD needs no credential and must not hold one."""
    assert not _volume_mounts(services["dind"], "registry-auth")


# ── registry-auth-init.sh ───────────────────────────────────────────────────

_FAKE_HTPASSWD = """#!/bin/sh
# Stand-in for apache's htpasswd: records how it was called, emits a bcrypt-shaped line.
printf '%s\\n' "$*" >"$STUB_LOG/htpasswd.argv"
for user; do :; done
cat >"$STUB_LOG/htpasswd.stdin"
[ "${STUB_HTPASSWD_BAD:-}" = 1 ] && { echo "$user:\\$apr1\\$plain"; exit 0; }
printf '%s:$2y$05$%s\\n\\n' "$user" "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNO"
"""


@pytest.fixture
def stub_bin(tmp_path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    return d


def _write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)


def _run_auth_init(tmp_path, stub_bin, **env):
    _write_stub(stub_bin, "htpasswd", _FAKE_HTPASSWD)
    log = tmp_path / "log"
    log.mkdir(exist_ok=True)
    return subprocess.run(
        ["sh", str(AUTH_INIT_SCRIPT)],
        env={
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
            "AUTH_DIR": str(tmp_path / "auth"),
            "STUB_LOG": str(log),
            **env,
        },
        capture_output=True,
        text=True,
    )


def test_auth_init_generates_a_login_once_and_keeps_it(tmp_path, stub_bin):
    first = _run_auth_init(tmp_path, stub_bin)
    assert first.returncode == 0, first.stderr
    auth = tmp_path / "auth"
    password = (auth / "password").read_text()
    assert re.fullmatch(r"[A-Za-z0-9]{40}", password), "a 40-character random password"
    assert (auth / "username").read_text() == "embry0"
    assert (auth / "htpasswd").read_text().startswith("embry0:$2y$"), "the registry accepts bcrypt only"
    for name in ("username", "password", "htpasswd"):
        assert stat.S_IMODE((auth / name).stat().st_mode) == 0o444

    second = _run_auth_init(tmp_path, stub_bin)
    assert second.returncode == 0, second.stderr
    assert (auth / "password").read_text() == password, "a later run must keep the password the clients use"
    assert "kept the existing registry password" in second.stdout


def test_auth_init_hands_the_password_to_htpasswd_on_stdin(tmp_path, stub_bin):
    assert _run_auth_init(tmp_path, stub_bin).returncode == 0
    password = (tmp_path / "auth" / "password").read_text()
    argv = (tmp_path / "log" / "htpasswd.argv").read_text()
    assert password not in argv, "the password must never be on a command line"
    assert argv.split() == ["-niB", "embry0"]
    assert (tmp_path / "log" / "htpasswd.stdin").read_text() == password


def test_auth_init_refuses_a_hash_the_registry_cannot_read(tmp_path, stub_bin):
    result = _run_auth_init(tmp_path, stub_bin, STUB_HTPASSWD_BAD="1")
    assert result.returncode != 0
    assert "bcrypt" in result.stderr
    assert not (tmp_path / "auth" / "htpasswd").exists()


@pytest.mark.parametrize("username", ["a:b", "a b", "a$b"])
def test_auth_init_refuses_a_username_basic_auth_cannot_carry(tmp_path, stub_bin, username):
    result = _run_auth_init(tmp_path, stub_bin, REGISTRY_USERNAME=username)
    assert result.returncode != 0
    assert not (tmp_path / "auth" / "password").exists()


# ── push-images-to-registry.sh ──────────────────────────────────────────────

_FAKE_DOCKER = """#!/bin/sh
# Stand-in for the docker CLI: logs every call, answers like the real one.
echo "$*" >>"$STUB_LOG/docker.calls"
case "$1" in
  login) cat >"$STUB_LOG/login.stdin"; echo "Login Succeeded" ;;
  image) exit 0 ;;
  tag) exit 0 ;;
  push)
    case "$2" in *"${STUB_PUSH_FAIL:-<none>}"*) echo "denied: requested access to the resource is denied" >&2; exit 1 ;; esac
    echo "The push refers to repository [${2%:*}]"
    echo "25f1d6b1951a: Pushed"
    [ "${STUB_PUSH_NO_DIGEST:-}" = 1 ] && exit 0
    printf '%s: digest: sha256:%s size: 856\\n' "${2##*:}" "$(printf '%s' "$2" | sha256sum | cut -c1-64)"
    ;;
  *) echo "unexpected docker call: $*" >&2; exit 2 ;;
esac
"""


def _digest_for(dst: str) -> str:
    return "sha256:" + hashlib.sha256(dst.encode()).hexdigest()


def _read_pins(path: Path) -> dict[str, str]:
    """The pins as the orchestrator will read them: through its own parser."""
    from embry0.execution.image_registry import parse_image_pins

    assert path.exists(), f"the push did not write the pins file {path}"
    return parse_image_pins(path.read_text())


def _run_push(tmp_path, stub_bin, *, with_auth=True, **env):
    _write_stub(stub_bin, "docker", _FAKE_DOCKER)
    log = tmp_path / "log"
    log.mkdir(exist_ok=True)
    auth = tmp_path / "auth"
    if with_auth and not auth.exists():
        auth.mkdir()
        (auth / "username").write_text("embry0")
        (auth / "password").write_text("s3cret-from-the-volume")
    pins_dir = tmp_path / "pins"
    pins_dir.mkdir(exist_ok=True)
    full_env = {
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "PINS_FILE": str(pins_dir / "pins"),
        **({"REGISTRY_AUTH_DIR": str(auth)} if with_auth else {}),
        **env,
    }
    result = subprocess.run(["sh", str(PUSH_SCRIPT)], env=full_env, capture_output=True, text=True)
    calls = (log / "docker.calls").read_text().splitlines() if (log / "docker.calls").exists() else []
    return result, calls


def test_push_logs_in_before_it_pushes_with_the_password_on_stdin(tmp_path, stub_bin):
    result, calls = _run_push(tmp_path, stub_bin, IMAGES="embry0-proxy")
    assert result.returncode == 0, result.stderr
    assert calls[0] == "login 127.0.0.1:5001 --username embry0 --password-stdin", calls
    assert all("s3cret" not in c for c in calls), "the password must never be on a command line"
    assert (tmp_path / "log" / "login.stdin").read_text() == "s3cret-from-the-volume"
    assert calls.index("push 127.0.0.1:5001/embry0-proxy:latest") > 0


def test_push_records_each_digest_the_push_reported(tmp_path, stub_bin):
    result, _ = _run_push(tmp_path, stub_bin, IMAGES="embry0-proxy embry0-sandbox")
    assert result.returncode == 0, result.stderr
    pins = _read_pins(tmp_path / "pins" / "pins")
    assert pins == {
        "embry0-proxy:latest": _digest_for("127.0.0.1:5001/embry0-proxy:latest"),
        "embry0-sandbox:latest": _digest_for("127.0.0.1:5001/embry0-sandbox:latest"),
    }, "each pin must be the digest that image's own push reported"
    assert stat.S_IMODE((tmp_path / "pins" / "pins").stat().st_mode) == 0o444
    assert sorted(p.name for p in (tmp_path / "pins").iterdir()) == ["pins"], "no temp file left behind"


def test_push_by_default_pins_every_image_the_orchestrator_requires(tmp_path, stub_bin):
    """The orchestrator refuses to start without these pins; the default push
    list must produce every one of them."""
    from embry0.api.app import _required_image_pins

    result, _ = _run_push(tmp_path, stub_bin)
    assert result.returncode == 0, result.stderr
    pins = _read_pins(tmp_path / "pins" / "pins")
    assert _required_image_pins() <= pins.keys(), sorted(_required_image_pins() - pins.keys())


def test_a_failed_push_keeps_the_previous_pins(tmp_path, stub_bin):
    previous = "embry0-proxy:latest sha256:" + "1" * 64 + "\n"
    (tmp_path / "pins").mkdir()
    (tmp_path / "pins" / "pins").write_text(previous)

    result, _ = _run_push(tmp_path, stub_bin, IMAGES="embry0-proxy embry0-sandbox", STUB_PUSH_FAIL="embry0-sandbox")

    assert result.returncode != 0
    assert (tmp_path / "pins" / "pins").read_text() == previous, "a partial run must not replace the pins"


def test_a_push_that_reports_no_digest_fails(tmp_path, stub_bin):
    result, _ = _run_push(tmp_path, stub_bin, IMAGES="embry0-proxy", STUB_PUSH_NO_DIGEST="1")
    assert result.returncode != 0
    assert "reported no digest" in result.stderr
    assert not (tmp_path / "pins" / "pins").exists()


def test_a_missing_login_stops_before_any_push(tmp_path, stub_bin):
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "username").write_text("embry0")
    result, calls = _run_push(tmp_path, stub_bin, IMAGES="embry0-proxy")
    assert result.returncode != 0
    assert "registry password missing" in result.stderr
    assert calls == [], "nothing may be pushed without the login"


def test_the_throwaway_login_is_removed_on_exit(tmp_path, stub_bin):
    (tmp_path / "tmp").mkdir()
    result, _ = _run_push(tmp_path, stub_bin, IMAGES="embry0-proxy", TMPDIR=str(tmp_path / "tmp"))
    assert result.returncode == 0, result.stderr
    assert list((tmp_path / "tmp").iterdir()) == []


def test_without_an_auth_dir_the_push_does_not_log_in(tmp_path, stub_bin):
    """Outside compose the script still works as before against an open registry."""
    result, calls = _run_push(tmp_path, stub_bin, with_auth=False, IMAGES="embry0-proxy")
    assert result.returncode == 0, result.stderr
    assert not any(c.startswith("login") for c in calls)
