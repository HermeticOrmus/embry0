"""#572: an env ANTHROPIC_API_KEY serves an api-key run.

With ``DEFAULT_AUTH_MODE=api_key`` and the key only in the root ``.env``, every
job failed with ERR_CREDENTIAL_MISSING: the env step of the credential resolver
read only OAuth sources for Anthropic. And with a key in the registry only, the
agent's up-front auth check still demanded the env string. Both halves are
pinned here.
"""

from __future__ import annotations

from types import SimpleNamespace

from embry0.services.credentials.providers import env_fallback
from embry0.services.credentials.resolver import CredentialResolver
from tests.unit.services.credentials.test_resolve_llm import LlmRepo
from tests.unit.services.credentials.test_resolver import FakeSecrets

_BOTH = {"ANTHROPIC_API_KEY": "sk-ant-env", "CLAUDE_CODE_OAUTH_TOKEN": "oat-env"}


def test_api_key_mode_is_served_the_env_api_key_not_the_oauth_token():
    got = env_fallback("anthropic", _BOTH, SimpleNamespace(default_auth_mode="api_key"), credentials_file="/none")
    assert got == ("llm_api_key", "sk-ant-env", "ANTHROPIC_API_KEY"), (
        f"an api-key deployment must be served ANTHROPIC_API_KEY, never the OAuth token; got {got}"
    )


def test_api_key_mode_without_the_key_gets_nothing():
    got = env_fallback(
        "anthropic", {"CLAUDE_CODE_OAUTH_TOKEN": "oat-env"}, SimpleNamespace(default_auth_mode="api_key")
    )
    assert got is None, f"an OAuth token must not stand in for a missing API key: {got}"


def test_oauth_mode_still_prefers_the_oauth_token():
    got = env_fallback("anthropic", _BOTH, SimpleNamespace(default_auth_mode="oauth"), credentials_file="/none")
    assert got == ("llm_oauth", "oat-env", "CLAUDE_CODE_OAUTH_TOKEN"), got


async def test_resolver_serves_the_env_api_key_to_an_api_key_deployment():
    resolver = CredentialResolver(
        LlmRepo({}),
        FakeSecrets(),
        SimpleNamespace(default_auth_mode="api_key"),
        environ={"ANTHROPIC_API_KEY": "sk-ant-env"},
        credentials_file="/no/such/credentials.json",
    )

    got = await resolver.resolve_llm("example-org/example-repo", "anthropic")

    assert (got.kind, got.secret, got.label, got.source) == ("llm_api_key", "sk-ant-env", "ANTHROPIC_API_KEY", "env")
    assert resolver.env_fallback_keys() == ["ANTHROPIC_API_KEY"]


def test_a_resolved_api_key_account_satisfies_the_up_front_check():
    """A registry key arrives as the resolved kind; the env string may be empty."""
    from embry0.agents.resolver import resolve_agent_invocation
    from tests.unit.agents.test_resolver_extended import _base_args

    inv = resolve_agent_invocation(
        **_base_args(
            global_defaults={"execution_mode": "sdk", "auth_mode": "api_key"},
            credentials={"api_key": "", "oauth_token": "", "anthropic_kind": "llm_api_key"},
        )
    )
    assert inv.auth_mode == "api_key"


def test_a_resolved_oauth_account_satisfies_the_up_front_check():
    from embry0.agents.resolver import resolve_agent_invocation
    from tests.unit.agents.test_resolver_extended import _base_args

    inv = resolve_agent_invocation(
        **_base_args(credentials={"api_key": "", "oauth_token": "", "anthropic_kind": "llm_oauth"})
    )
    assert inv.auth_mode == "oauth"
