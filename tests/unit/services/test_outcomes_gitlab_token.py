"""#424: the MR fallback must open MRs for a deployment that has only a registry or namespace GitLab token."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embry0.services._executor_outcomes import _open_change_request
from embry0.services.issue_executor import IssueExecutor


@pytest.fixture(autouse=True)
def _no_gitlab_env(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN__GROUP", raising=False)


def _executor(resolver):
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._config = SimpleNamespace(gitlab_token="", gitlab_url="https://gitlab.example.com", gitlab_ca_bundle="")
    ex._credential_resolver = resolver
    ex._repos = MagicMock(forge_for=AsyncMock(return_value="gitlab"))
    return ex


async def _open(ex):
    return await _open_change_request(ex, repo="group/proj", branch="embry0/x", title="t", body="b", target_branch="")


async def test_mr_fallback_uses_resolver_not_config_token():
    resolver = MagicMock(resolve_forge=AsyncMock(return_value=SimpleNamespace(secret="glpat-reg")))
    ex = _executor(resolver)
    with patch(
        "embry0.services.gitlab_mrs.ensure_mr_for_branch",
        new=AsyncMock(return_value="https://gitlab.example.com/mr/1"),
    ) as ensure:
        url = await _open(ex)
    assert url == "https://gitlab.example.com/mr/1", f"MR fallback skipped despite a registry token: {url!r}"
    assert ensure.await_args.kwargs["client"]._credential_resolver is resolver


async def test_mr_fallback_uses_namespace_env_token_without_resolver(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN__GROUP", "glpat-ns")
    ex = _executor(None)
    with patch(
        "embry0.services.gitlab_mrs.ensure_mr_for_branch",
        new=AsyncMock(return_value="https://gitlab.example.com/mr/2"),
    ):
        url = await _open(ex)
    assert url == "https://gitlab.example.com/mr/2", f"MR fallback skipped despite GITLAB_TOKEN__GROUP: {url!r}"


async def test_mr_fallback_still_skips_when_nothing_configured():
    ex = _executor(None)
    with patch("embry0.services.gitlab_mrs.ensure_mr_for_branch", new=AsyncMock()) as ensure:
        assert await _open(ex) is None
    ensure.assert_not_awaited()


async def test_base_branch_lookup_gets_a_gitlab_client_from_the_resolver():
    from embry0.services._executor_outcomes import _base_branch_for_change_request

    resolver = MagicMock(resolve_forge=AsyncMock(return_value=SimpleNamespace(secret="glpat-reg")))
    ex = _executor(resolver)
    with patch("embry0.services.base_branch.resolve_base_branch", new=AsyncMock(return_value="develop")) as rbb:
        assert await _base_branch_for_change_request(ex, "group/proj", {}) == "develop"
    client = rbb.await_args.kwargs["gitlab_client"]
    assert client is not None, "base-branch lookup skipped the forge despite a registry token"
    assert client._credential_resolver is resolver
