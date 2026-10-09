"""git-proxy bearer auth and admin enrollment (#521: a bearer opens git routes, never a credential)."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from embry0.execution.proxy.git_proxy import create_git_proxy_app

from .git_forge import ADMIN as RIG_ADMIN
from .git_forge import ProxyRig, RecordingUpstream

ADMIN = "test-admin-secret-not-real"
PAT = "ghp_PAT_TESTING_ONLY"

# Tokens used in tests must be >= 32 chars per enrollment validation
TOK_S1 = "tok-s1-" + "a" * 30  # 37 chars
TOK_S2 = "tok-s2-" + "b" * 30  # 37 chars
TOK_A = "token-A-" + "a" * 30  # 38 chars
TOK_B = "token-B-" + "b" * 30  # 38 chars


@pytest.fixture
async def client():
    app = create_git_proxy_app(github_token=PAT, admin_token=ADMIN)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


@pytest.fixture
async def rig():
    async with ProxyRig(create_git_proxy_app(github_token=PAT, admin_token=RIG_ADMIN), RecordingUpstream()) as r:
        yield r


async def _rig_enroll(rig: ProxyRig, sandbox_id: str, token: str) -> None:
    await rig.enroll(sandbox_id, token, git_host="127.0.0.1")


async def _unenroll(rig: ProxyRig, sandbox_id: str) -> None:
    resp = await rig.client.delete(f"/admin/enroll/{sandbox_id}", headers={"X-Admin-Token": RIG_ADMIN})
    assert resp.status == 200


async def test_health_open(client):
    resp = await client.get("/health")
    assert resp.status == 200


async def test_git_route_without_bearer_rejected(rig):
    resp = await rig.client.get("/git/acme/widgets.git/info/refs?service=git-upload-pack")
    assert resp.status == 403
    assert "not enrolled" in await resp.text()
    assert rig.upstream.seen == []


async def test_git_route_with_unenrolled_bearer_rejected(rig):
    resp = await rig.fetch_refs("bogus")
    assert resp.status == 403
    assert rig.upstream.seen == []


async def test_enroll_requires_admin_header(client):
    resp = await client.post("/admin/enroll", json={"sandbox_id": "s1", "sandbox_token": TOK_S1})
    assert resp.status == 401


async def test_enroll_with_wrong_admin_rejected(client):
    resp = await client.post(
        "/admin/enroll",
        json={"sandbox_id": "s1", "sandbox_token": TOK_S1},
        headers={"X-Admin-Token": "wrong"},
    )
    assert resp.status == 401


async def test_enrolled_bearer_is_forwarded(rig):
    await _rig_enroll(rig, "s1", TOK_S1)
    resp = await rig.fetch_refs(TOK_S1)
    assert resp.status == 200
    assert len(rig.upstream.seen) == 1


async def test_multi_tenant_both_work(rig):
    for sid, tok in [("s1", TOK_S1), ("s2", TOK_S2)]:
        await _rig_enroll(rig, sid, tok)
    for tok in (TOK_S1, TOK_S2):
        resp = await rig.fetch_refs(tok)
        assert resp.status == 200


async def test_unenroll_revokes(rig):
    await _rig_enroll(rig, "s1", TOK_S1)
    await _unenroll(rig, "s1")

    resp = await rig.fetch_refs(TOK_S1)
    assert resp.status == 403
    assert rig.upstream.seen == []


async def test_create_app_rejects_empty_admin():
    with pytest.raises(ValueError, match="admin_token is required"):
        create_git_proxy_app(github_token="x", admin_token="")


# --- I-1: re-enrollment test ---


async def test_re_enroll_replaces_old_token(rig):
    """Enrolling s1 with token B replaces token A; A → 403, B → 200; unenroll removes both and the grant."""
    await _rig_enroll(rig, "s1", TOK_A)
    assert (await rig.fetch_refs(TOK_A)).status == 200

    await _rig_enroll(rig, "s1", TOK_B)
    assert (await rig.fetch_refs(TOK_A)).status == 403
    assert (await rig.fetch_refs(TOK_B)).status == 200

    await _unenroll(rig, "s1")
    assert (await rig.fetch_refs(TOK_B)).status == 403
    assert rig.client.app["git_grant_by_id"] == {}, "the grant must end with its enrollment"


# --- I-2: token length floor ---


async def test_enroll_rejects_short_sandbox_token(client):
    """sandbox_token shorter than 32 chars must be rejected with 400."""
    resp = await client.post(
        "/admin/enroll",
        json={"sandbox_id": "s1", "sandbox_token": "x"},
        headers={"X-Admin-Token": ADMIN},
    )
    assert resp.status == 400


# --- I-3: sandbox_id format validation ---


async def test_enroll_rejects_malformed_sandbox_id(client):
    """sandbox_id with path-traversal characters must be rejected with 400."""
    resp = await client.post(
        "/admin/enroll",
        json={"sandbox_id": "../../../etc/passwd", "sandbox_token": TOK_S1},
        headers={"X-Admin-Token": ADMIN},
    )
    assert resp.status == 400

    # Also reject a sandbox_id longer than 64 characters
    long_id = "a" * 65
    resp = await client.post(
        "/admin/enroll",
        json={"sandbox_id": long_id, "sandbox_token": TOK_S1},
        headers={"X-Admin-Token": ADMIN},
    )
    assert resp.status == 400
