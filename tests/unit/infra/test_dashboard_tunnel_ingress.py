"""The dashboard tunnel's ingress follows DASHBOARD_PUBLIC_HOSTNAME and fails closed.

The tunnel (`cloudflared-dashboard`) reads a config rendered by the one-shot
`cloudflared-dashboard-config` compose service from
``infra/cloudflared-dashboard/config.yml.template``, with the same script and
hostname rules as the frontend's public vhost. Rule 0 sends that hostname to
nginx; everything else is a 404 at the tunnel. Unset or invalid: the render
refuses, leaves no config, and the tunnel never starts. A catch-all ingress
would hand the internet to nginx's permissive LAN vhost.

Everything here is read from the compose file rather than restated: which
script the render service runs and with which arguments, where its output
lands, and where cloudflared reads its config. A tree without the render
service (the state these tests were written against) is emulated as compose
would run it, with cloudflared reading its bind-mounted tracked file as is.
"""

from __future__ import annotations

import io
import re
import shlex
import shutil
import subprocess
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.unit.infra.test_dashboard_public_vhost import INVALID_HOSTNAMES, RENDERER, VAR

REPO_ROOT = Path(__file__).resolve().parents[3]
INFRA = REPO_ROOT / "infra"
COMPOSE = INFRA / "docker-compose.yml"
TUNNEL_DIR = INFRA / "cloudflared-dashboard"
PROVISION = TUNNEL_DIR / "provision.sh"

TUNNEL = "cloudflared-dashboard"
RENDER_SERVICE = "cloudflared-dashboard-config"
PUBLIC_HOST = "dashboard.example.org"
OTHER_HOST = "build-box.example.net"
PLACEHOLDER = "${" + VAR + "}"


def _services() -> dict[str, Any]:
    services: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())["services"]
    return services


@dataclass
class Mount:
    source: str
    target: str
    read_only: bool

    @property
    def is_bind(self) -> bool:
        return self.source.startswith((".", "/"))

    def host_path(self) -> Path:
        assert self.is_bind, f"{self.source} is a named volume"
        return (INFRA / self.source).resolve()


def _mounts(service: dict[str, Any]) -> list[Mount]:
    out = []
    for spec in service.get("volumes", []):
        parts = spec.split(":")
        out.append(Mount(parts[0], parts[1], len(parts) > 2 and "ro" in parts[2].split(",")))
    return out


def _tunnel_config_path() -> str:
    command = _services()[TUNNEL]["command"]
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    return argv[argv.index("--config") + 1]


def _tunnel_config_mount() -> tuple[Mount, str]:
    """The tunnel mount holding its --config file, and the file's path inside it."""
    path = _tunnel_config_path()
    for m in _mounts(_services()[TUNNEL]):
        if path == m.target or path.startswith(m.target.rstrip("/") + "/"):
            return m, path[len(m.target) :].lstrip("/")
    raise AssertionError(f"no {TUNNEL} mount holds its --config {path}")


def _ingress(config: dict[str, Any]) -> list[dict[str, Any]]:
    ingress: list[dict[str, Any]] = config["ingress"]
    return ingress


# ---------------------------------------------------------------------------
# Static tier: run what compose runs before the tunnel, on the host.
# ---------------------------------------------------------------------------


@dataclass
class TunnelRender:
    returncode: int
    stderr: str
    config: dict[str, Any] | None


def _render_tunnel(tmp_path: Path, hostname: str | None, *, stale: str | None = None) -> TunnelRender:
    """What the tunnel would find at its --config path after compose's startup."""
    config_mount, relative = _tunnel_config_mount()
    services = _services()
    if RENDER_SERVICE not in services:
        # Before the render service: cloudflared reads a bind-mounted file.
        assert config_mount.is_bind, f"{TUNNEL} config comes from nowhere"
        source = config_mount.host_path()
        if config_mount.target != _tunnel_config_path():
            source = source / relative
        return TunnelRender(0, "", yaml.safe_load(source.read_text()))

    render = services[RENDER_SERVICE]
    volume_dir = tmp_path / "volume"
    volume_dir.mkdir()
    if stale is not None:
        (volume_dir / relative).write_text(stale)
    # Map the render container's paths onto the host: bind mounts to their
    # sources, the volume it shares with the tunnel to a temp dir.
    mapping: dict[str, Path] = {}
    for m in _mounts(render):
        if m.is_bind:
            mapping[m.target] = m.host_path()
        else:
            assert m.source == config_mount.source, (
                f"{RENDER_SERVICE} writes to {m.source}, the tunnel reads {config_mount.source}"
            )
            mapping[m.target] = volume_dir

    def host(arg: str) -> str:
        for target, path in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
            if arg == target:
                return str(path)
            if arg.startswith(target.rstrip("/") + "/"):
                return str(path / arg[len(target) :].lstrip("/"))
        return arg

    env = {"PATH": "/usr/bin:/bin"}
    if hostname is not None:
        env[VAR] = hostname
    proc = subprocess.run([host(a) for a in render["entrypoint"]], env=env, capture_output=True, text=True, timeout=30)
    rendered = volume_dir / relative
    config = yaml.safe_load(rendered.read_text()) if rendered.exists() else None
    return TunnelRender(proc.returncode, proc.stderr, config)


def test_the_tunnel_sends_the_public_hostname_to_nginx_and_404s_the_rest(tmp_path: Path) -> None:
    result = _render_tunnel(tmp_path, PUBLIC_HOST)
    assert result.returncode == 0, result.stderr
    assert result.config is not None, "the render left no config for the tunnel"
    ingress = _ingress(result.config)
    assert ingress == [
        {"hostname": PUBLIC_HOST, "service": "http://frontend:80"},
        {"service": "http_status:404"},
    ], f"{VAR}={PUBLIC_HOST} gives the tunnel ingress {ingress}"
    assert result.config.get("credentials-file") == "/etc/cloudflared/creds.json"
    assert "tunnel" not in result.config, "the tunnel UUID belongs on the CLI, not in the config"


@pytest.mark.parametrize("hostname", [None, ""], ids=["unset", "empty"])
def test_an_unset_hostname_leaves_the_tunnel_no_config(tmp_path: Path, hostname: str | None) -> None:
    result = _render_tunnel(tmp_path, hostname)
    assert result.config is None, (
        f"with {VAR} unset the tunnel still gets ingress {_ingress(result.config)}; it must not start"
    )
    assert result.returncode != 0, "the render must fail so the tunnel, which waits for it, never starts"
    assert VAR in result.stderr, f"the refusal does not name {VAR}: {result.stderr!r}"


@pytest.mark.parametrize("value", INVALID_HOSTNAMES)
def test_an_invalid_hostname_leaves_the_tunnel_no_config(tmp_path: Path, value: str) -> None:
    result = _render_tunnel(tmp_path, value)
    assert result.config is None and result.returncode != 0, f"{VAR}={value!r} gave the tunnel a config"
    assert VAR in result.stderr


def test_a_refused_render_removes_the_previous_config(tmp_path: Path) -> None:
    """A tunnel restarted without its render (reboot, --no-deps) must find
    nothing rather than a config from an earlier value."""
    stale = "ingress:\n  - hostname: stale.example.org\n    service: http://frontend:80\n  - service: http_status:404\n"
    result = _render_tunnel(tmp_path, None, stale=stale)
    assert result.config is None, "a refused render left the previous config in place"


def test_the_tracked_tunnel_config_names_no_hostname() -> None:
    config_mount, _ = _tunnel_config_mount()
    assert not config_mount.is_bind, (
        f"{TUNNEL} reads its config straight from the tracked {config_mount.source}, so a hostname "
        f"would have to be written into the repo"
    )
    hostnames: dict[str, list[Any]] = {}
    for path in sorted(TUNNEL_DIR.iterdir()):
        if path.suffix in {".yml", ".yaml", ".template"}:
            config = yaml.safe_load(path.read_text()) or {}
            hostnames[path.name] = [r.get("hostname") for r in config.get("ingress", []) if "hostname" in r]
    assert hostnames, f"no tunnel config template under {TUNNEL_DIR}"
    assert all(names == [PLACEHOLDER] for names in hostnames.values()), (
        f"tracked tunnel configs name hostnames {hostnames}; only the {PLACEHOLDER} placeholder belongs in the repo"
    )


def test_the_tunnel_waits_for_a_successful_render() -> None:
    services = _services()
    assert RENDER_SERVICE in services, f"no {RENDER_SERVICE} service renders the tunnel's ingress"
    render, tunnel = services[RENDER_SERVICE], services[TUNNEL]
    assert tunnel["depends_on"][RENDER_SERVICE]["condition"] == "service_completed_successfully", (
        "the tunnel must not start unless the render succeeded"
    )
    assert render.get("profiles") == tunnel.get("profiles"), "the render must run whenever the tunnel's profile does"
    assert render.get("restart") == "no"
    assert render["environment"][VAR] == "${" + VAR + ":-}", (
        f"{VAR} must come from interpolation with an empty default: `:?` would make every compose "
        f"command fail on deployments with no public dashboard"
    )
    config_mount, _ = _tunnel_config_mount()
    assert config_mount.read_only, "the tunnel must not be able to rewrite its own ingress"
    assert tunnel.get("labels", {}).get("embry0.dashboard-public-hostname") == "${" + VAR + ":-}", (
        "cloudflared reads its config once: without the hostname in its config hash, changing the "
        "variable re-renders the file but leaves the running tunnel on the old rules"
    )


def test_the_tunnel_renders_with_the_frontend_script() -> None:
    """One script, one set of hostname rules, for the vhost and the tunnel."""
    services = _services()
    assert RENDER_SERVICE in services, f"no {RENDER_SERVICE} service renders the tunnel's ingress"
    render = services[RENDER_SERVICE]
    entrypoint = render["entrypoint"]
    assert "--require" in entrypoint, "without --require an unset hostname renders nothing and exits 0"
    assert entrypoint[0] == "sh", f"{RENDER_SERVICE} must run its script with sh: {entrypoint}"
    scripts = [m for m in _mounts(render) if m.is_bind and m.target == entrypoint[1]]
    assert [m.host_path() for m in scripts] == [RENDERER.resolve()], (
        f"{RENDER_SERVICE} runs {entrypoint[1]} from {[m.source for m in scripts]}, not the frontend's {RENDERER.name}"
    )
    templates = [m for m in _mounts(render) if m.is_bind and m.host_path().name == "config.yml.template"]
    assert templates and templates[0].target in entrypoint


# ---------------------------------------------------------------------------
# provision.sh: same variable, same rules, checked before Cloudflare is called.
# ---------------------------------------------------------------------------


@dataclass
class Provision:
    returncode: int
    output: str
    curl_calls: list[str]


def _provision(tmp_path: Path, *, env_value: str | None = None, file_value: str | None = None) -> Provision:
    """Run a copy of provision.sh against a fake `curl` that records its calls
    and answers every Cloudflare request with one existing object."""
    tunnel_dir = tmp_path / "infra" / "cloudflared-dashboard"
    tunnel_dir.mkdir(parents=True)
    shutil.copy(PROVISION, tunnel_dir / PROVISION.name)
    (tunnel_dir / "creds.json").write_text("{}")
    if RENDERER.exists():
        (tmp_path / "frontend" / "nginx").mkdir(parents=True)
        shutil.copy(RENDERER, tmp_path / "frontend" / "nginx" / RENDERER.name)
    if file_value is not None:
        (tmp_path / "infra" / ".env").write_text(f"{VAR}={file_value}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "curl.log"
    fake = bin_dir / "curl"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\necho \'{{"result":[{{"id":"t1"}}]}}\'\n')
    fake.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CF_ACCOUNT_ID": "acct",
        "CF_ZONE_ID": "zone",
        "CF_API_TOKEN": "token",
    }
    if env_value is not None:
        env[VAR] = env_value
    proc = subprocess.run(
        ["bash", str(tunnel_dir / PROVISION.name)], env=env, capture_output=True, text=True, timeout=60
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return Provision(proc.returncode, proc.stdout + proc.stderr, calls)


def _dns_names(calls: list[str]) -> list[str]:
    return re.findall(r'"name":"([^"]*)"', "\n".join(c for c in calls if "dns_records" in c))


def test_provision_refuses_to_run_without_the_hostname(tmp_path: Path) -> None:
    result = _provision(tmp_path)
    assert result.returncode != 0 and not result.curl_calls, (
        f"provision.sh called Cloudflare with no {VAR}: {result.curl_calls}"
    )
    assert VAR in result.output


@pytest.mark.parametrize("value", ["*.example.org", "https://dashboard.example.org", "localhost"])
def test_provision_refuses_an_invalid_hostname(tmp_path: Path, value: str) -> None:
    result = _provision(tmp_path, env_value=value)
    assert result.returncode != 0 and not result.curl_calls, f"provision.sh accepted {VAR}={value!r}"


def test_provision_points_the_cname_at_the_hostname_from_infra_env(tmp_path: Path) -> None:
    result = _provision(tmp_path, file_value=f'"{PUBLIC_HOST}"')
    assert result.returncode == 0, result.output
    assert _dns_names(result.curl_calls) == [PUBLIC_HOST], (
        f"the DNS record was written for {_dns_names(result.curl_calls)}, not {VAR} from infra/.env"
    )


def test_provision_prefers_the_environment_over_infra_env(tmp_path: Path) -> None:
    result = _provision(tmp_path, env_value=PUBLIC_HOST, file_value=OTHER_HOST)
    assert result.returncode == 0, result.output
    assert _dns_names(result.curl_calls) == [PUBLIC_HOST], (
        f"the DNS record was written for {_dns_names(result.curl_calls)}, not {VAR} from the environment"
    )


# ---------------------------------------------------------------------------
# Real containers: the render image and the cloudflared image from compose.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def docker_ok() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode != 0:
        pytest.skip("Docker daemon not reachable")


def _docker(*args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, timeout=120)


def _tar(files: dict[str, Path]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for target, source in files.items():
            info = tar.gettarinfo(str(source), arcname=target.lstrip("/"))
            info.uid = info.gid = 0
            with source.open("rb") as fh:
                tar.addfile(info, fh)
    return buf.getvalue()


def _cloudflared_user() -> str:
    """The tunnel's `user:`, with compose's default for an unset CF_RUN_USER."""
    user = str(_services()[TUNNEL].get("user", ""))
    return user.split(":-", 1)[1].rstrip("}") if user.startswith("${") else user


@dataclass
class TunnelContainers:
    render_rc: int
    render_output: str
    validate: str
    rules: dict[str, str]


def _run_tunnel_containers(hostname: str | None) -> TunnelContainers:
    """Render into a throwaway volume as compose would, then ask the tunnel's
    own image, as its own user, to validate and match the result."""
    services = _services()
    tunnel = services[TUNNEL]
    config_mount, _ = _tunnel_config_mount()
    volume = f"probe-dashboard-tunnel-{uuid.uuid4().hex[:12]}"
    assert _docker("volume", "create", volume).returncode == 0
    try:
        if RENDER_SERVICE in services:
            render = services[RENDER_SERVICE]
            files = {m.target: m.host_path() for m in _mounts(render) if m.is_bind}
            (vol_target,) = [m.target for m in _mounts(render) if not m.is_bind]
            env_args = ["-e", f"{VAR}={hostname}"] if hostname is not None else []
            # Bind mounts arrive as a tar on stdin, so this works against a
            # remote daemon too; the entrypoint is compose's, verbatim.
            proc = _docker(
                "run", "--rm", "-i", "--network", "none", "-v", f"{volume}:{vol_target}", *env_args,
                "--entrypoint", "sh", render["image"], "-c", 'tar -x -C / && exec "$@"', "--",
                *render["entrypoint"],
                stdin=_tar(files),
            )  # fmt: skip
            render_rc, render_out = proc.returncode, (proc.stdout + proc.stderr).decode(errors="replace")
        else:
            # Before the render service: the tracked file, as the bind mount gave it.
            source = config_mount.host_path()
            proc = _docker(
                "run", "--rm", "-i", "--network", "none", "-v", f"{volume}:/v", "--entrypoint", "sh",
                "docker:27-cli", "-c", f"cat > /v/{Path(_tunnel_config_path()).name}",
                stdin=source.read_bytes(),
            )  # fmt: skip
            render_rc, render_out = 0, ""
        target = config_mount.target if not config_mount.is_bind else str(Path(_tunnel_config_path()).parent)

        def cloudflared(*args: str) -> str:
            proc = _docker(
                "run", "--rm", "--network", "none", "--user", _cloudflared_user(),
                "-v", f"{volume}:{target}:ro", tunnel["image"],
                "tunnel", "--config", _tunnel_config_path(), "ingress", *args,
            )  # fmt: skip
            return f"rc={proc.returncode}\n" + (proc.stdout + proc.stderr).decode(errors="replace")

        rules = {host: cloudflared("rule", f"https://{host}/") for host in (PUBLIC_HOST, OTHER_HOST)}
        return TunnelContainers(render_rc, render_out, cloudflared("validate"), rules)
    finally:
        _docker("volume", "rm", "-f", volume)


@pytest.mark.requires_docker
def test_cloudflared_validates_the_rendered_ingress(docker_ok: None) -> None:
    run = _run_tunnel_containers(PUBLIC_HOST)
    assert run.render_rc == 0, run.render_output
    assert run.validate.startswith("rc=0") and "OK" in run.validate, run.validate
    public, other = run.rules[PUBLIC_HOST], run.rules[OTHER_HOST]
    assert "Matched rule #0" in public and "http://frontend:80" in public, (
        f"cloudflared does not send {PUBLIC_HOST} to nginx:\n{public}"
    )
    assert "Matched rule #1" in other and "http_status:404" in other, f"cloudflared does not 404 another host:\n{other}"


@pytest.mark.requires_docker
def test_cloudflared_has_no_config_when_the_hostname_is_unset(docker_ok: None) -> None:
    run = _run_tunnel_containers(None)
    assert run.render_rc != 0, f"the render accepted an unset {VAR}:\n{run.render_output}"
    assert VAR in run.render_output
    assert not run.validate.startswith("rc=0"), f"cloudflared found a config to run:\n{run.validate}"
    for host, out in run.rules.items():
        assert "Matched rule" not in out, f"cloudflared routed {host} with no hostname set:\n{out}"
