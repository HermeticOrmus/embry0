"""SandboxManager injects the LLM account resolve_llm_for_job returns."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.execution.sandbox_manager import SandboxInitError, SandboxManager
from embry0.safety.error_codes import ErrorCode
from embry0.services.credentials.resolver import CredentialUnavailableError, ResolvedCredential
from tests.unit.execution.test_sandbox_manager import (  # noqa: F401
    _stub_egress_unless_overridden,
    _stub_isolation_unless_overridden,
)
from tests.unit.execution.test_sandbox_manager_credentials import _docker, _proxy, _resolved


def _llm(provider: str, kind: str, secret: str, *, cid: str = "cred-llm") -> ResolvedCredential:
    return ResolvedCredential(
        id=cid,
        kind=kind,
        provider=provider,
        host="api.x.ai" if provider == "xai" else "api.anthropic.com",
        label=f"{provider}-{kind}",
        secret=secret,
        git_author_name=None,
        git_author_email=None,
        source="registry",
        trace=(f"global: -> {provider}-{kind}",),
    )


def _manager(resolved: dict[str, ResolvedCredential] | Exception) -> tuple[SandboxManager, MagicMock, MagicMock]:
    resolver = MagicMock()
    if isinstance(resolved, Exception):
        resolver.resolve_llm_for_job = AsyncMock(side_effect=resolved)
    else:
        resolver.resolve_llm_for_job = AsyncMock(return_value=resolved)
    resolver.resolve_forge = AsyncMock(return_value=_resolved())
    pm = _proxy()
    mgr = SandboxManager(docker=_docker(), proxy_manager=pm, credential_resolver=resolver)
    return mgr, resolver, pm


def _env(mgr: SandboxManager) -> dict[str, str]:
    return mgr._docker.build_run_cmd.call_args.kwargs["env"]


@pytest.mark.parametrize(
    ("provider", "kind", "secret", "present", "absent"),
    [
        ("anthropic", "llm_oauth", "oat-secret", {"CLAUDE_CODE_OAUTH_TOKEN": "oat-secret"}, ["ANTHROPIC_API_KEY"]),
        ("anthropic", "llm_api_key", "key-secret", {"ANTHROPIC_API_KEY": "key-secret"}, ["CLAUDE_CODE_OAUTH_TOKEN"]),
        ("xai", "llm_api_key", "xai-secret", {"XAI_API_KEY": "xai-secret"}, []),
        ("xai", "llm_oauth", "refresh-secret", {}, ["XAI_API_KEY"]),
    ],
)
async def test_llm_injection_table(provider, kind, secret, present, absent):
    cred = _llm(provider, kind, secret, cid="cred-row")
    mgr, resolver, pm = _manager({provider: cred})
    caller_env = {"ANTHROPIC_API_KEY": "stale-key", "CLAUDE_CODE_OAUTH_TOKEN": "stale-oat", "XAI_API_KEY": "stale-xai"}
    container_id, _ = await mgr.create("job-1", repo="acme/widgets", env=caller_env, llm_providers=[provider])
    env = _env(mgr)
    for key, value in present.items():
        assert env[key] == value
    for key in absent:
        assert key not in env
    assert secret not in str(mgr._docker.build_run_cmd.call_args.args)
    remembered = mgr.last_llm_credentials(container_id)
    assert remembered[provider].id == "cred-row"
    resolver.resolve_llm_for_job.assert_awaited_once()
    if provider == "xai" and kind == "llm_oauth":
        assert pm.enroll_sandbox.await_args.kwargs["xai_credential_id"] == "cred-row"
    else:
        assert pm.enroll_sandbox.await_args.kwargs["xai_credential_id"] is None


async def test_pinned_unavailable_fails_before_any_container():
    mgr, _, pm = _manager(CredentialUnavailableError("credential 'claude-a' is pinned for acme/widgets but disabled"))
    with pytest.raises(SandboxInitError) as raised:
        await mgr.create("job-1", repo="acme/widgets", llm_providers=["anthropic"])
    assert raised.value.error_code == ErrorCode.CREDENTIAL_UNAVAILABLE
    mgr._docker.build_run_cmd.assert_not_called()
    pm.enroll_sandbox.assert_not_awaited()


async def test_child_sandbox_reuses_the_parent_choice():
    cred = _llm("anthropic", "llm_oauth", "oat-secret")
    mgr, resolver, _ = _manager({"anthropic": cred})
    await mgr.create("job-child", repo="acme/widgets", llm_providers=["anthropic"], parent_job_id="job-parent")
    assert resolver.resolve_llm_for_job.await_args.kwargs["parent_job_id"] == "job-parent"
    assert resolver.resolve_llm_for_job.await_args.kwargs["job_id"] == "job-child"
