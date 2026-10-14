"""Records proxy: a sandbox reads its own job's repository's records, and only those."""

from __future__ import annotations

import httpx
import pytest
from aiohttp.test_utils import TestClient

from embry0.execution.proxy.records_proxy import create_records_proxy_app, normalize_records_repo

_ADMIN = "test-admin-secret-not-real"
TOKEN_A = "sandbox-a-bearer-token-at-least-32-chars-long"
TOKEN_B = "sandbox-b-bearer-token-at-least-32-chars-long"
REPO_A = "acme/widgets"
REPO_B = "group/sub/secret"


class _Resp:
    def __init__(self, status_code: int = 200, content: bytes = b'{"records": []}'):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": "application/json"}


class _FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response = _Resp()
        self.raises: Exception | None = None

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if self.raises is not None:
            raise self.raises
        return self.response

    async def aclose(self):
        pass


@pytest.fixture
async def proxy(aiohttp_client):
    app = create_records_proxy_app("http://10.0.0.1:8000", "svc-read-key", _ADMIN)
    client = await aiohttp_client(app)
    fake = _FakeUpstream()
    app["http_client"] = fake
    return client, fake


async def _enroll(client: TestClient, sandbox_id: str, token: str, scope: object) -> tuple[int, dict]:
    body: dict = {"sandbox_id": sandbox_id, "sandbox_token": token}
    if scope is not None:
        body["allowed_repos"] = scope
    resp = await client.post("/admin/enroll", json=body, headers={"X-Admin-Token": _ADMIN})
    return resp.status, await resp.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_construction_needs_upstream_and_key() -> None:
    with pytest.raises(ValueError):
        create_records_proxy_app("", "k", _ADMIN)
    with pytest.raises(ValueError):
        create_records_proxy_app("http://x", "", _ADMIN)


def test_repo_normalization() -> None:
    assert normalize_records_repo("group/sub/project") == "group/sub/project"
    for bad in (" a/b", "one-segment", "a/../b", "a//b", "a/b/", "/a/b", "a/b\n", "a/b?x=1", "a/%2e%2e"):
        assert normalize_records_repo(bad) is None, bad


@pytest.mark.asyncio
async def test_enrollment_echoes_the_scope_and_refuses_bad_ones(proxy) -> None:
    client, _ = proxy
    assert await _enroll(client, "s1", TOKEN_A, [REPO_A]) == (200, {"status": "enrolled", "allowed_repos": [REPO_A]})
    assert await _enroll(client, "s2", TOKEN_B, []) == (200, {"status": "enrolled", "allowed_repos": []})
    for bad in (None, "acme/widgets", [REPO_A, REPO_B], ["not-a-repo"], [1]):
        status, _ = await _enroll(client, "s3", "x" * 40, bad)
        assert status == 400, f"enrollment with allowed_repos={bad!r} must be refused"


@pytest.mark.asyncio
async def test_a_sandbox_reads_only_its_enrolled_repository(proxy) -> None:
    """The heart of the scoping: sandbox A cannot reach repo B's records by any path."""
    client, fake = proxy
    await _enroll(client, "sa", TOKEN_A, [REPO_A])
    await _enroll(client, "sb", TOKEN_B, [REPO_B])

    attempts = [
        "/records",
        "/records/outage",
        "/records?repo=group/sub/secret",
        f"/records/..%2F..%2F{REPO_B.replace('/', '%2F')}%2Frecords",
        "/repos/group/sub/secret/records",
        "/api/v1/repos/group/sub/secret/records",
        "/records/outage/../../group/sub/secret/records",
    ]
    for path in attempts:
        await client.get(path, headers=_bearer(TOKEN_A))
    assert fake.calls, "the legitimate reads must have reached upstream"
    for call in fake.calls:
        assert call["url"].startswith(f"http://10.0.0.1:8000/api/v1/repos/{REPO_A}/records"), (
            f"sandbox A's request reached {call['url']}: the repository must come from A's enrollment"
        )
        assert REPO_B not in call["url"] and "secret" not in str(call["params"])

    fake.calls.clear()
    resp = await client.get("/records/outage", headers=_bearer(TOKEN_B))
    assert resp.status == 200
    assert fake.calls[-1]["url"] == f"http://10.0.0.1:8000/api/v1/repos/{REPO_B}/records/outage"
    assert fake.calls[-1]["headers"] == {"Authorization": "Bearer svc-read-key"}


@pytest.mark.asyncio
async def test_unscoped_and_unenrolled_callers_get_nothing(proxy) -> None:
    client, fake = proxy
    await _enroll(client, "s0", TOKEN_A, [])
    assert (await client.get("/records", headers=_bearer(TOKEN_A))).status == 403
    assert (await client.get("/records", headers=_bearer(TOKEN_B))).status == 401
    assert (await client.get("/records")).status == 401
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_re_enrollment_replaces_the_scope(proxy) -> None:
    client, fake = proxy
    await _enroll(client, "s1", TOKEN_A, [REPO_B])
    await _enroll(client, "s1", TOKEN_A, [REPO_A])
    await client.get("/records", headers=_bearer(TOKEN_A))
    assert fake.calls[-1]["url"].endswith(f"/repos/{REPO_A}/records")
    resp = await client.delete("/admin/enroll/s1", headers={"X-Admin-Token": _ADMIN})
    assert resp.status == 200
    assert (await client.get("/records", headers=_bearer(TOKEN_A))).status == 401


@pytest.mark.asyncio
async def test_parameters_and_slugs_are_checked(proxy) -> None:
    client, fake = proxy
    await _enroll(client, "s1", TOKEN_A, [REPO_A])
    ok = await client.get("/records", params={"kind": "rca", "q": "cache", "limit": "5"}, headers=_bearer(TOKEN_A))
    assert ok.status == 200
    assert fake.calls[-1]["params"] == {"kind": "rca", "q": "cache", "limit": "5"}
    await client.get("/records/outage", params={"revision": "2"}, headers=_bearer(TOKEN_A))
    assert fake.calls[-1]["params"] == {"revision": "2"}
    n = len(fake.calls)
    assert (await client.get("/records", params={"repo": REPO_B}, headers=_bearer(TOKEN_A))).status == 400
    assert (await client.get("/records/outage", params={"kind": "x"}, headers=_bearer(TOKEN_A))).status == 400
    assert (await client.get("/records/Bad%20Slug", headers=_bearer(TOKEN_A))).status == 400
    assert (await client.get("/records/a/revisions", headers=_bearer(TOKEN_A))).status == 404
    assert (await client.put("/records/a", json={}, headers=_bearer(TOKEN_A))).status == 405
    assert len(fake.calls) == n, "a refused request must not reach upstream"


@pytest.mark.asyncio
async def test_upstream_answers_pass_through_and_failures_map(proxy) -> None:
    client, fake = proxy
    await _enroll(client, "s1", TOKEN_A, [REPO_A])
    fake.response = _Resp(404, b'{"detail": "no record"}')
    resp = await client.get("/records/missing", headers=_bearer(TOKEN_A))
    assert resp.status == 404 and (await resp.json())["detail"] == "no record"
    fake.raises = httpx.ConnectError("down")
    assert (await client.get("/records", headers=_bearer(TOKEN_A))).status == 502
    fake.raises = httpx.ReadTimeout("slow")
    assert (await client.get("/records", headers=_bearer(TOKEN_A))).status == 504
