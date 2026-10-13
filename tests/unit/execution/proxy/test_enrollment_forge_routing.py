"""Spec §3.2: a forge payload reaches git-proxy and that forge's API proxy, nothing else."""

from unittest.mock import AsyncMock

import pytest

from embry0.execution.proxy.manager import ProxyManager


async def _manager_all_proxies(monkeypatch):
    pm = ProxyManager(docker=AsyncMock(), proxy_admin_token="x" * 20)
    pm._http = object()
    pm.git_proxy_url = "http://git-proxy:9101"
    pm.github_proxy_url = "http://github-proxy:9103"
    pm.gitlab_proxy_url = "http://gitlab-proxy:9105"
    pm.xai_proxy_url = "http://xai-proxy:9106"
    captured = []

    async def fake_exec(**kwargs):
        captured.append(kwargs)
        # A current proxy echoes the repo scope it stored (#520), and the
        # git-proxy its whole grant (#521).
        body = kwargs.get("body") or {}
        return {k: body[k] for k in ("allowed_repos", "git_upstream", "git_push", "git_push_branches") if k in body}

    monkeypatch.setattr(pm, "_exec_admin_request", fake_exec)
    return pm, captured


def _body_for(captured, container):
    return next(c["body"] for c in captured if c["container"] == container)


_BARE_KEYS = {"sandbox_id", "sandbox_token", "ttl_seconds"}
# #520: the other forge's API proxy is enrolled with an empty repo scope.
_SCOPED_EMPTY_KEYS = _BARE_KEYS | {"allowed_repos"}


async def test_gitlab_payload_never_reaches_github_proxy(monkeypatch):
    pm, captured = await _manager_all_proxies(monkeypatch)
    await pm.enroll_sandbox(
        "sb-1", github_token="glpat-SECRET", git_host="gitlab.example.com", git_username="oauth2", forge="gitlab"
    )
    assert _body_for(captured, "git-proxy")["github_token"] == "glpat-SECRET"
    assert _body_for(captured, "gitlab-proxy")["github_token"] == "glpat-SECRET"
    assert "github_token" not in _body_for(captured, "github-proxy")
    assert "github_token" not in _body_for(captured, "xai-proxy")
    assert set(_body_for(captured, "github-proxy")) == _SCOPED_EMPTY_KEYS
    assert _body_for(captured, "github-proxy")["allowed_repos"] == []
    assert set(_body_for(captured, "xai-proxy")) == _BARE_KEYS
    assert {c["container"] for c in captured} == {"git-proxy", "github-proxy", "gitlab-proxy", "xai-proxy"}


async def test_github_payload_never_reaches_gitlab_proxy(monkeypatch):
    pm, captured = await _manager_all_proxies(monkeypatch)
    await pm.enroll_sandbox("sb-2", github_token="ghp_SECRET", forge="github")
    assert _body_for(captured, "github-proxy")["github_token"] == "ghp_SECRET"
    assert "github_token" not in _body_for(captured, "gitlab-proxy")
    assert set(_body_for(captured, "gitlab-proxy")) == _SCOPED_EMPTY_KEYS
    assert _body_for(captured, "gitlab-proxy")["allowed_repos"] == []
    assert set(_body_for(captured, "xai-proxy")) == _BARE_KEYS


async def test_no_forge_keeps_broadcast(monkeypatch):
    pm, captured = await _manager_all_proxies(monkeypatch)
    await pm.enroll_sandbox("sb-3", github_token="ghp_SECRET", git_host="github.com", git_username="x-access-token")
    assert all(c["body"].get("github_token") == "ghp_SECRET" for c in captured)
    assert all(c["body"].get("git_host") == "github.com" for c in captured)
    assert all(c["body"].get("git_username") == "x-access-token" for c in captured)


async def test_unknown_forge_fails_closed(monkeypatch):
    """A forge this manager cannot route must not fall back to broadcasting a secret."""
    pm, captured = await _manager_all_proxies(monkeypatch)
    with pytest.raises(ValueError, match="unknown forge 'bitbucket'"):
        await pm.enroll_sandbox("sb-4", github_token="SECRET", forge="bitbucket")
    assert captured == []
