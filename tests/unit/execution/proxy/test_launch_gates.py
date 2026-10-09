"""#424: proxies launch on 'any credential for this forge', not only on the plain token."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.execution.proxy.manager import ProxyManager


@pytest.fixture(autouse=True)
def _mock_backend_resolution(monkeypatch):
    """start() resolves `minio`/`orchestrator` via DNS for the network-plumbing
    proxies; that DNS does not exist in unit tests. Same stub as
    test_manager_dind.py.
    """
    monkeypatch.setattr(
        "embry0.execution.proxy.manager.socket.gethostbyname",
        lambda host: "10.99.0.1",
    )


async def _started(monkeypatch, **kw):
    pm = ProxyManager(docker=MagicMock(run_cmd=AsyncMock(return_value="")), proxy_admin_token="x" * 20)
    pm._http = MagicMock()  # skip opening a real aiohttp session
    launched = []

    async def fake_launch(*, name, env, attach_internet):
        launched.append(name)

    monkeypatch.setattr(pm, "_launch", fake_launch)
    await pm.start(**kw)
    return launched


async def test_registry_only_gitlab_launches_git_and_gitlab_proxies(monkeypatch):
    launched = await _started(
        monkeypatch,
        has_gitlab_credential=True,
        has_github_credential=False,
        gitlab_api_base="https://gitlab.example.com/api/v4",
    )
    assert "git-proxy" in launched and "gitlab-proxy" in launched and "github-proxy" not in launched


async def test_none_keeps_token_gates(monkeypatch):
    launched = await _started(monkeypatch, github_token="ghp_x")
    assert "git-proxy" in launched and "github-proxy" in launched and "gitlab-proxy" not in launched
    launched = await _started(monkeypatch)
    assert "git-proxy" not in launched
