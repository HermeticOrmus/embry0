from unittest.mock import AsyncMock, MagicMock

import httpx

from embry0.execution.github_client import GitHubClient
from embry0.execution.gitlab_client import GitLabClient, make_gitlab_client
from embry0.services.credentials.resolver import CredentialMissingError, ResolvedCredential


def _resolved(secret, provider="github"):
    return ResolvedCredential(
        id="cred-1",
        kind="forge_token",
        provider=provider,
        host="h",
        label="l",
        secret=secret,
        git_author_name=None,
        git_author_email=None,
        source="registry",
        trace=(),
    )


async def _capture(client):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["private"] = request.headers.get("PRIVATE-TOKEN")
        return httpx.Response(200, json={})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


async def test_github_request_uses_registry_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN__ACME", "ghp_env")
    resolver = MagicMock(resolve_forge=AsyncMock(return_value=_resolved("ghp_registry")))
    client = GitHubClient(default_token="ghp_default", credential_resolver=resolver)
    seen = await _capture(client)
    await client.get("/repos/acme/widgets", repo="acme/widgets")
    assert seen["auth"] == "Bearer ghp_registry"
    resolver.resolve_forge.assert_awaited_once_with("acme/widgets", "github")


async def test_github_without_resolver_uses_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN__ACME", "ghp_env")
    client = GitHubClient(default_token="ghp_default")
    seen = await _capture(client)
    await client.get("/repos/acme/widgets", repo="acme/widgets")
    assert seen["auth"] == "Bearer ghp_env"


async def test_missing_in_registry_sends_no_auth_header():
    resolver = MagicMock(resolve_forge=AsyncMock(side_effect=CredentialMissingError("x")))
    client = GitHubClient(default_token="ghp_default", credential_resolver=resolver)
    seen = await _capture(client)
    await client.get("/repos/acme/widgets", repo="acme/widgets")
    assert seen["auth"] is None


async def test_gitlab_private_token_from_registry():
    resolver = MagicMock(resolve_forge=AsyncMock(return_value=_resolved("glpat-registry", provider="gitlab")))
    client = make_gitlab_client(
        MagicMock(gitlab_token="", gitlab_url="https://gitlab.example.com", gitlab_ca_bundle=""),
        credential_resolver=resolver,
    )
    assert isinstance(client, GitLabClient)
    seen = await _capture(client)
    await client.get(client.project_path("group/proj"), repo="group/proj")
    assert seen["private"] == "glpat-registry"
    resolver.resolve_forge.assert_awaited_once_with("group/proj", "gitlab")
