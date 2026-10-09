"""git-proxy basics: health, the retired credential endpoint, and what crosses the proxy (#521)."""

from __future__ import annotations

import pytest
import structlog
from aiohttp.test_utils import TestClient

from embry0.execution.proxy.git_proxy import create_git_proxy_app

from .git_forge import ADMIN, FORGE_TOKEN, INFO_REFS, ProxyRig, RecordingUpstream, enroll

BEARER = "sandbox-bearer-" + "c" * 40


@pytest.fixture
async def git_client(aiohttp_client) -> TestClient:
    return await aiohttp_client(create_git_proxy_app(github_token=FORGE_TOKEN, admin_token=ADMIN))


async def test_health(git_client: TestClient):
    resp = await git_client.get("/health")
    assert resp.status == 200
    assert (await resp.json())["proxy"] == "git"


async def test_git_credentials_refuses_an_enrolled_sandbox_without_a_credential(git_client: TestClient):
    """The helper endpoint used to answer ``password=<token>``; now it answers 410 and logs it."""
    await enroll(git_client, "s1", BEARER)
    with structlog.testing.capture_logs() as logs:
        resp = await git_client.get("/git-credentials", headers={"Authorization": f"Bearer {BEARER}"})
    body = await resp.text()
    assert resp.status == 410
    assert FORGE_TOKEN not in body and "password=" not in body
    assert [e["sandbox_id"] for e in logs if e["event"] == "git_proxy_credentials_refused"] == ["s1"]


async def test_git_credentials_without_a_bearer_is_401(git_client: TestClient):
    assert (await git_client.get("/git-credentials")).status == 401


async def test_only_git_headers_cross_the_proxy_either_way():
    upstream = RecordingUpstream(extra_headers={"Set-Cookie": "session=forge", "WWW-Authenticate": "Basic"})
    async with ProxyRig(create_git_proxy_app(FORGE_TOKEN, ADMIN), upstream) as rig:
        await rig.enroll("s1", BEARER, git_host="127.0.0.1", github_token=FORGE_TOKEN)
        resp = await rig.fetch_refs(
            BEARER,
            **{
                "Git-Protocol": "version=2",
                "User-Agent": "git/2.43.0",
                "Cookie": "sandbox=1",
                "X-Forwarded-For": "10.0.0.1",
                "X-Custom": "nope",
            },
        )
        assert resp.status == 200
        assert await resp.read() == upstream.body
        assert resp.headers["Content-Type"] == "application/x-git-upload-pack-advertisement"
        assert "Set-Cookie" not in resp.headers and "WWW-Authenticate" not in resp.headers

    sent = upstream.seen[0].headers
    assert sent["Git-Protocol"] == "version=2" and sent["User-Agent"] == "git/2.43.0"
    for dropped in ("Cookie", "X-Forwarded-For", "X-Custom"):
        assert dropped not in sent, f"{dropped} crossed the proxy"
    assert BEARER not in sent["Authorization"], "the sandbox bearer reached the forge"


async def test_a_forge_redirect_is_not_followed():
    """A renamed repository redirects to another path: following it would take the credential out of scope."""
    upstream = RecordingUpstream(status=301, extra_headers={"Location": "http://127.0.0.1:1/acme/everything.git"})
    async with ProxyRig(create_git_proxy_app(FORGE_TOKEN, ADMIN), upstream) as rig:
        await rig.enroll("s1", BEARER, git_host="127.0.0.1")
        with structlog.testing.capture_logs() as logs:
            resp = await rig.fetch_refs(BEARER)
        assert resp.status == 502
        assert "Location" not in resp.headers
        assert "redirect" in await resp.text()
    assert len(upstream.seen) == 1
    assert "git_proxy_upstream_redirect" in [e["event"] for e in logs]


async def test_a_forge_401_becomes_a_403_with_a_reason():
    """git answers a 401 by prompting for a username, which hides the cause."""
    upstream = RecordingUpstream(status=401, extra_headers={"WWW-Authenticate": 'Basic realm="forge"'})
    async with ProxyRig(create_git_proxy_app(FORGE_TOKEN, ADMIN), upstream) as rig:
        await rig.enroll("s1", BEARER, git_host="127.0.0.1")
        with structlog.testing.capture_logs() as logs:
            resp = await rig.fetch_refs(BEARER)
        assert resp.status == 403
        assert "WWW-Authenticate" not in resp.headers
        assert "refused the configured credential" in await resp.text()
    assert "git_proxy_upstream_unauthorized" in [e["event"] for e in logs]


async def test_an_unreachable_forge_is_a_502(aiohttp_client):
    client = await aiohttp_client(create_git_proxy_app(FORGE_TOKEN, ADMIN))
    # Port 9 (discard) on loopback: nothing listens, the connect is refused.
    await enroll(client, "s1", BEARER, allowed_repos=["acme/widgets"], git_upstream="http://127.0.0.1:9")
    resp = await client.get(INFO_REFS.format(repo="acme/widgets"), headers={"Authorization": f"Bearer {BEARER}"})
    assert resp.status == 502
    assert "could not reach the forge" in await resp.text()
