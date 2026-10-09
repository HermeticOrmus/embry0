"""Guard: the env templates cover what compose interpolates, and the stack
parses without the optional webhook tunnel (#531).

Compose reads infra/.env when it runs from infra/ (infra/deploy.sh, the
systemd unit, CONTRIBUTING), and the repo-root .env when `embry0 start` passes
it with --env-file. A variable missing from a template is found by the first
operator whose `docker compose` refuses to parse, and a required variable in
an opt-in service makes the opt-in mandatory: compose checks `${VAR:?}` even
in a service whose profile is off.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "infra" / "docker-compose.yml"
INFRA_ENV_EXAMPLE = REPO_ROOT / "infra" / ".env.example"
ROOT_ENV_EXAMPLE = REPO_ROOT / ".env.example"

# `$VAR` or `${VAR…}`, but not the `$$` escape compose passes through.
_REFERENCE = re.compile(r"(?<!\$)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z_][A-Za-z0-9_]*))")
# `${VAR:?msg}` or `${VAR?msg}`: compose refuses to parse while VAR is unset.
_REQUIRED = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*):?\?")
_ENTRY = re.compile(r"^#?\s*([A-Z_][A-Z0-9_]*)=", re.MULTILINE)
_SET = re.compile(r"^([A-Z_][A-Z0-9_]*)=", re.MULTILINE)


def _interpolated(text: str) -> set[str]:
    return {a or b for a, b in _REFERENCE.findall(text)}


def _strings(node: Any) -> list[str]:
    """Every string in a parsed YAML subtree, keys included."""
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return [node] if isinstance(node, str) else []


def _template(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing: an operator has no template for it"
    return path.read_text()


def _services() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_the_reference_pattern_skips_the_dollar_escape():
    assert _interpolated('a: "${A:-x}" b: $B c: "$$C" d: "$${D}"') == {"A", "B"}
    assert set(_REQUIRED.findall('"${A:?set it}" "${B?x}" "${C:-}" "$${D:?x}"')) == {"A", "B"}


def test_infra_env_example_has_an_entry_for_every_interpolated_variable():
    referenced = _interpolated(COMPOSE.read_text())
    documented = set(_ENTRY.findall(_template(INFRA_ENV_EXAMPLE)))
    missing = sorted(referenced - documented)
    assert not missing, (
        f"infra/docker-compose.yml interpolates {missing}, which infra/.env.example does not "
        f"list. Add each one, set or commented out at its default."
    )


def test_every_required_variable_is_set_in_both_templates():
    required = set(_REQUIRED.findall(COMPOSE.read_text()))
    assert required, "found no ${VAR:?} in the compose file: the pattern no longer matches it"
    for template in (INFRA_ENV_EXAMPLE, ROOT_ENV_EXAMPLE):
        missing = sorted(required - set(_SET.findall(_template(template))))
        assert not missing, (
            f"compose refuses to parse without {missing}, but {template.relative_to(REPO_ROOT)} "
            f"does not have an uncommented line for them, so a copy of it leaves them unset."
        )


def test_no_optional_service_requires_a_variable():
    offenders = {
        name: sorted({m for s in _strings(svc) for m in _REQUIRED.findall(s)})
        for name, svc in _services().items()
        if svc.get("profiles")
    }
    offenders = {name: found for name, found in offenders.items() if found}
    assert not offenders, (
        f"services behind a profile require variables: {offenders}. Compose checks a required "
        f"variable even while the profile is off, so the whole stack refuses to parse without it."
    )


def test_the_webhook_tunnel_is_opt_in():
    assert _services()["cloudflared"].get("profiles") == ["webhook-tunnel"], (
        "cloudflared must run only with the webhook-tunnel profile: without one, the stack "
        "cannot start without a Cloudflare tunnel token."
    )


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _compose_available(), reason="needs the docker compose CLI")
def test_compose_parses_with_only_the_required_variables(tmp_path: Path) -> None:
    """The real parser, fed exactly what a copied template must hold.

    Parse-only: `config` talks to no daemon, and DOCKER_HOST points at nothing
    to keep it that way.
    """
    env_file = tmp_path / "env"
    env_file.write_text("".join(f"{name}=x\n" for name in sorted(set(_REQUIRED.findall(COMPOSE.read_text())))))
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    env["DOCKER_HOST"] = f"unix://{tmp_path}/no-daemon.sock"
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--env-file", str(env_file), "config", "--services"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"compose refused to parse:\n{result.stderr}"
    services = set(result.stdout.split())
    assert "orchestrator" in services, result.stdout
    assert "cloudflared" not in services, "cloudflared starts by default; it must be opt-in"
