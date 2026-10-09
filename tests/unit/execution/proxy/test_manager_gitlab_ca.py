"""ProxyManager delivers the private CA to the gitlab-proxy (spec 1A §3.2)."""

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.execution.proxy.manager import ProxyManager

CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIDFTCCAf2g\nAoUvla6\n-----END CERTIFICATE-----\n"


@pytest.fixture(autouse=True)
def _mock_backend_resolution(monkeypatch):
    """start() resolves `minio`/`orchestrator` via DNS for the Phase 1.5
    proxies; that DNS does not exist in unit tests. Same stub as
    test_manager_dind.py.
    """
    monkeypatch.setattr(
        "embry0.execution.proxy.manager.socket.gethostbyname",
        lambda host: "10.99.0.1",
    )


def _manager() -> ProxyManager:
    docker = MagicMock()
    docker.run_cmd = AsyncMock(return_value="")
    docker.build_rm_cmd = MagicMock(return_value=["docker", "rm", "x"])
    docker.build_network_cmd = MagicMock(return_value=["docker", "network", "x"])
    docker.build_run_proxy_cmd = MagicMock(return_value=(["docker", "run", "x"], "/dev/null"))
    mgr = ProxyManager(docker, proxy_admin_token="admin-secret")
    mgr._http = MagicMock()
    return mgr


def _gitlab_env(docker: MagicMock) -> dict[str, str]:
    for call in docker.build_run_proxy_cmd.call_args_list:
        if call.kwargs["name"] == "gitlab-proxy":
            return dict(call.kwargs["env"])
    raise AssertionError("gitlab-proxy was never launched")


@pytest.mark.asyncio
async def test_ca_pem_rides_the_proxy_env_base64_encoded():
    """docker --env-file truncates at the first newline, so the PEM is encoded."""
    mgr = _manager()
    await mgr.start(gitlab_token="glpat-x", gitlab_api_base="https://gl.example/api/v4", gitlab_ca_pem=CA_PEM)
    env = _gitlab_env(mgr._docker)
    assert "\n" not in env["GITLAB_CA_PEM"]
    assert base64.b64decode(env["GITLAB_CA_PEM"]).decode() == CA_PEM


@pytest.mark.asyncio
async def test_no_ca_pem_means_no_env_key():
    mgr = _manager()
    await mgr.start(gitlab_token="glpat-x", gitlab_api_base="https://gl.example/api/v4")
    assert "GITLAB_CA_PEM" not in _gitlab_env(mgr._docker)
