"""#524: env vars that redirect sandbox traffic or load code into sandbox processes.

Every process in a sandbox inherits the agent's LLM credential, and the git
credential helper fetches the forge token over plain http. A repo env var that
routes that traffic through a proxy, or runs code at process start, defeats
both. Each such key must be refused at the API and dropped at both injection
points (user env rows and profile env_defaults), whatever its case.

The key lists are literal on purpose: a test that read them from
auth_provider would shrink with the registry it is meant to pin.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from embry0.api.schemas.environment import EnvVarInput
from embry0.execution.sandbox_manager import SandboxManager
from embry0.workflows.issue_to_pr.nodes._common import _filter_user_env_for_sandbox

# Autouse: every sandbox on sandbox-restricted gets the isolation chain (#554),
# which the docker mock cannot write.
from tests.unit.execution.test_sandbox_manager import _stub_isolation_unless_overridden  # noqa: F401

# Refused whatever the value.
BLOCKED_KEYS = [
    # proxy routing
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "FTP_PROXY",
    # TLS verification and session-key logging
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "SSL_CERT_DIR",
    "PYTHONHTTPSVERIFY",
    "SSLKEYLOGFILE",
    # dynamic loader
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    # binary lookup and per-user config
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "SHELL",
    "CURL_HOME",
    # shell startup
    "BASH_ENV",
    "SHELLOPTS",
    "BASHOPTS",
    "PS4",
    # interpreter startup
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONBREAKPOINT",
    "PERL5OPT",
    "PERL5LIB",
    "PERLLIB",
    "PERL5DB",
    "RUBYOPT",
    "RUBYLIB",
    # the GIT_ namespace
    "GIT_DIR",
    "GIT_EXEC_PATH",
    "GIT_TEMPLATE_DIR",
    "GIT_EXTERNAL_DIFF",
    "GIT_PROXY_COMMAND",
    "GIT_SSL_NO_VERIFY",
    "GIT_TRACE",
    # the agent CLI's namespace
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_ENV_FILE",
    "CLAUDE_CODE_SHELL",
    "CLAUDE_CODE_USE_BEDROCK",
    # the agent CLI's runtime namespace
    "BUN_OPTIONS",
    "BUN_CONFIG_VERBOSE_FETCH",
]

JVM_OPTION_KEYS = ["JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "JAVA_OPTS", "MAVEN_OPTS", "GRADLE_OPTS"]

# Settable, but only to memory flags: (key, value) pairs that must pass ...
GUARDED_OK = [
    ("NODE_OPTIONS", "--max-old-space-size=4096"),
    ("NODE_OPTIONS", "--max-old-space-size=4096 --max-semi-space-size=64"),
    ("NODE_OPTIONS", "--max_old_space_size=2048"),
    ("NODE_OPTIONS", ""),
] + [
    (key, value)
    for key in JVM_OPTION_KEYS
    for value in ("-Xmx2g", "-Xms512m -Xmx4G -Xss1m", "-XX:MaxRAMPercentage=75.0", "-XX:MaxMetaspaceSize=512m")
]

# ... and pairs that must not.
GUARDED_BAD = [
    ("NODE_OPTIONS", "--require /tmp/hook.js"),
    ("NODE_OPTIONS", "-r /tmp/hook.js"),
    ("NODE_OPTIONS", "--import=data:text/javascript,0"),
    ("NODE_OPTIONS", "--max-old-space-size=4096 --require /tmp/hook.js"),
    ("NODE_OPTIONS", "--inspect=0.0.0.0:9229"),
    ("NODE_OPTIONS", "--max-old-space-size=lots"),
] + [
    (key, value)
    for key in JVM_OPTION_KEYS
    for value in (
        "-javaagent:/tmp/agent.jar",
        "-Xmx2g -javaagent:/tmp/agent.jar",
        "-XX:OnOutOfMemoryError=/tmp/hook.sh",
        "-Dhttps.proxyHost=proxy.invalid",
        "@/tmp/argfile",
        "-agentlib:jdwp=transport=dt_socket,server=y",
        "-Xbootclasspath/a:/tmp/x.jar",
    )
]

# Deliberately left settable (see the PR for each reason). These pin that the
# namespaces above did not swallow neighbours that only share a few letters.
STILL_SETTABLE = [
    ("ENV", "production"),  # sh reads ENV only for interactive shells
    ("NODE_PATH", "src"),  # fallback lookup only; cannot shadow node_modules
    ("PYTHONUNBUFFERED", "1"),
    ("LD_SDK_KEY", "sdk-key"),  # shares LD_ with the loader variables
    ("TRUST_PROXY", "1"),  # ends in _PROXY
    ("HTTP_PORT", "8080"),  # shares HTTP_ with HTTP_PROXY
    ("GITEA_URL", "https://forge.invalid"),  # starts with GIT, not GIT_
    ("BUNDLE_GEMFILE", "Gemfile"),  # starts with BUN, not BUN_
    ("PIP_INDEX_URL", "https://pypi.invalid/simple"),  # registry config: open policy question
    ("npm_config_registry", "https://npm.invalid/"),
]


def _spellings(key: str) -> list[str]:
    """Upper, lower and alternating case: env names are compared case-insensitively."""
    alternating = "".join(c.lower() if i % 2 else c.upper() for i, c in enumerate(key))
    return sorted({key, key.lower(), alternating})


BLOCKED_SPELLINGS = [s for key in BLOCKED_KEYS for s in _spellings(key)]
GUARDED_OK_SPELLINGS = [(s, v) for key, v in GUARDED_OK for s in _spellings(key)]
GUARDED_BAD_SPELLINGS = [(s, v) for key, v in GUARDED_BAD for s in _spellings(key)]


# --- API: EnvVarInput -------------------------------------------------------


@pytest.mark.parametrize("key", BLOCKED_SPELLINGS)
def test_api_refuses_blocked_key(key: str) -> None:
    try:
        EnvVarInput(key=key, value="x")
    except ValidationError as exc:
        errors = [(err["loc"], err["msg"]) for err in exc.errors()]
        assert len(errors) == 1 and errors[0][0] == ("key",), f"{key} refused, but not on the key: {errors}"
        assert "reserved" in errors[0][1], f"{key} refused, but not as a reserved key: {errors}"
    else:
        pytest.fail(f"API accepted {key!r}, which hooks every sandbox process (#524)")


@pytest.mark.parametrize(("key", "value"), GUARDED_OK_SPELLINGS)
def test_api_accepts_guarded_key_with_memory_flags(key: str, value: str) -> None:
    assert EnvVarInput(key=key, value=value).value == value


@pytest.mark.parametrize(("key", "value"), GUARDED_BAD_SPELLINGS)
def test_api_refuses_guarded_key_outside_memory_flags(key: str, value: str) -> None:
    try:
        EnvVarInput(key=key, value=value)
    except ValidationError as exc:
        errors = [(err["loc"], err["msg"]) for err in exc.errors()]
        assert len(errors) == 1 and errors[0][0] == ("value",), f"{key} refused, but not on the value: {errors}"
        assert "may only carry" in errors[0][1], f"{key}={value!r} refused for the wrong reason: {errors}"
        assert value not in errors[0][1], f"the refusal message repeats the value of {key}"
    else:
        pytest.fail(f"API accepted {key}={value!r}; only memory flags may pass (#524)")


@pytest.mark.parametrize(("key", "value"), STILL_SETTABLE)
def test_api_still_accepts_neighbouring_keys(key: str, value: str) -> None:
    assert EnvVarInput(key=key, value=value).key == key


# --- injection: user env rows -------------------------------------------------


def _rows(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"key": k, "value": v, "var_type": "config", "scope": "app"} for k, v in pairs]


def test_user_env_filter_drops_blocked_keys_in_every_case() -> None:
    env = _filter_user_env_for_sandbox(
        _rows([(k, "x") for k in BLOCKED_SPELLINGS] + [("SAFE_VAR", "1")]), qa_active=False
    )
    survivors = sorted(set(env) - {"SAFE_VAR"})
    assert survivors == [], f"user env injection let hook keys through: {survivors}"
    assert env == {"SAFE_VAR": "1"}


def test_user_env_filter_keeps_guarded_keys_with_memory_flags() -> None:
    for key, value in GUARDED_OK_SPELLINGS:
        env = _filter_user_env_for_sandbox(_rows([(key, value)]), qa_active=False)
        assert env == {key: value}, f"user env injection dropped {key}={value!r}"


def test_user_env_filter_drops_guarded_keys_outside_memory_flags() -> None:
    leaked = [
        (key, value)
        for key, value in GUARDED_BAD_SPELLINGS
        if _filter_user_env_for_sandbox(_rows([(key, value)]), qa_active=False)
    ]
    assert leaked == [], f"user env injection let these through: {leaked}"


def test_user_env_filter_keeps_neighbouring_keys() -> None:
    env = _filter_user_env_for_sandbox(_rows(STILL_SETTABLE), qa_active=False)
    assert env == dict(STILL_SETTABLE)


# --- injection: profile env_defaults -----------------------------------------


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> SandboxManager:
    from embry0.execution.runtime import egress as egress_mod

    enforcer = MagicMock()
    enforcer.container_ip = AsyncMock(return_value="172.20.0.9")
    enforcer.ensure_helper_image = AsyncMock()
    enforcer.apply = AsyncMock()
    enforcer.clear = AsyncMock()
    monkeypatch.setattr(egress_mod, "EgressEnforcer", MagicMock(return_value=enforcer))
    docker = MagicMock()
    docker.run_cmd = AsyncMock(return_value="container-abc123")
    docker.build_run_cmd = MagicMock(return_value=["docker", "run", "..."])
    docker.build_network_cmd = MagicMock(return_value=["docker", "network", "..."])
    proxy_mgr = MagicMock()
    proxy_mgr.enroll_sandbox = AsyncMock(return_value="tok-abc123")
    proxy_mgr.unenroll_sandbox = AsyncMock()
    return SandboxManager(docker=docker, proxy_manager=proxy_mgr)


async def _profile_env(manager: SandboxManager, env_defaults: dict[str, str]) -> dict[str, str]:
    await manager.create(job_id="job-524", profile={"env_defaults": env_defaults})
    return manager._docker.build_run_cmd.call_args.kwargs.get("env", {}) or {}


# Values no infrastructure default carries, so a survivor is unambiguous.
_MARK = "set-by-profile-524"


async def test_profile_env_defaults_drop_blocked_keys_in_every_case(manager: SandboxManager) -> None:
    env = await _profile_env(manager, {**{k: _MARK for k in BLOCKED_SPELLINGS}, "LANG": "C.UTF-8"})
    survivors = sorted(k for k, v in env.items() if v == _MARK)
    assert survivors == [], f"profile env_defaults let hook keys through: {survivors}"
    assert env.get("LANG") == "C.UTF-8"


async def test_profile_env_defaults_keep_guarded_keys_with_memory_flags(manager: SandboxManager) -> None:
    env = await _profile_env(manager, {"NODE_OPTIONS": "--max-old-space-size=4096", "maven_opts": "-Xmx2g"})
    assert env.get("NODE_OPTIONS") == "--max-old-space-size=4096"
    assert env.get("maven_opts") == "-Xmx2g"


async def test_profile_env_defaults_drop_guarded_keys_outside_memory_flags(manager: SandboxManager) -> None:
    env = await _profile_env(
        manager, {"NODE_OPTIONS": "--require /tmp/hook.js", "Java_Tool_Options": "-javaagent:/tmp/agent.jar"}
    )
    leaked = {k: v for k, v in env.items() if "hook.js" in v or "agent.jar" in v}
    assert leaked == {}, f"profile env_defaults let code-loading flags through: {sorted(leaked)}"
