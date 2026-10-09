"""#425: git-steering variables are infrastructure-owned."""

import pytest
from pydantic import ValidationError

from embry0.api.schemas.environment import EnvVarInput
from embry0.execution.auth_provider import RESERVED_ENV_KEYS, RESERVED_ENV_PREFIXES
from embry0.workflows.issue_to_pr.nodes._common import _filter_user_env_for_sandbox

GIT_KEYS = [
    "GIT_SSH_COMMAND",
    "GIT_SSH",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_TERMINAL_PROMPT",
    "GIT_CURL_VERBOSE",
    "GIT_TRACE_CURL",
]


def test_git_keys_reserved() -> None:
    assert set(GIT_KEYS) <= RESERVED_ENV_KEYS
    assert "GIT_CONFIG_" in RESERVED_ENV_PREFIXES


@pytest.mark.parametrize("key", GIT_KEYS + ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT", "git_ssh_command"])
def test_api_rejects_git_keys(key: str) -> None:
    with pytest.raises(ValidationError):
        EnvVarInput(key=key, value="x")


def test_sandbox_filter_drops_git_keys() -> None:
    rows = [
        {"key": k, "value": "x", "var_type": "config", "scope": "app"}
        for k in GIT_KEYS + ["GIT_CONFIG_GLOBAL", "SAFE_VAR"]
    ]
    env = _filter_user_env_for_sandbox(rows, qa_active=False)
    assert set(env) == {"SAFE_VAR"}
