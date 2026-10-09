"""#424: init's base-branch lookup must reach the forge with a namespace or registry GitLab token."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from embry0.workflows.issue_to_pr.nodes._init import _resolve_base_branch_for_job

_CONFIG = SimpleNamespace(
    gitlab_token="", gitlab_url="https://gitlab.example.com", gitlab_ca_bundle="", github_token=""
)


async def _gitlab_client_for(configurable):
    with patch(
        "embry0.services.base_branch.resolve_base_branch_with_source",
        new=AsyncMock(return_value=("develop", "forge")),
    ) as rbb:
        await _resolve_base_branch_for_job(
            repo="group/proj", forge="gitlab", override=None, configurable=configurable, embry0_config=_CONFIG
        )
    return rbb.await_args.kwargs["gitlab_client"]


async def test_namespace_env_token_builds_the_gitlab_client(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN__GROUP", "glpat-ns")
    assert await _gitlab_client_for({}) is not None, "lookup skipped despite GITLAB_TOKEN__GROUP"


async def test_registry_token_builds_the_gitlab_client(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN__GROUP", raising=False)
    resolver = MagicMock(resolve_forge=AsyncMock(return_value=SimpleNamespace(secret="glpat-reg")))
    client = await _gitlab_client_for({"credential_resolver": resolver})
    assert client is not None, "lookup skipped despite a registry token"
    assert client._credential_resolver is resolver


async def test_no_token_skips_the_gitlab_client(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN__GROUP", raising=False)
    assert await _gitlab_client_for({}) is None
