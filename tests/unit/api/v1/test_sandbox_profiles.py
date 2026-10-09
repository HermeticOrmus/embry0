from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config


@pytest.fixture
def app():
    config = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True)
    app = create_app(config)
    mock_profiles = MagicMock()
    mock_profiles.upsert = AsyncMock()
    mock_profiles.get = AsyncMock(
        return_value={"name": "python-3.12", "base_image": "img", "memory": "8g", "cpus": "4"}
    )
    mock_profiles.list = AsyncMock(return_value=[{"name": "python-3.12", "base_image": "img"}])
    mock_profiles.delete = AsyncMock()
    app.state.profiles_repo = mock_profiles
    return app


@pytest.mark.asyncio
async def test_list_profiles(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/sandbox-profiles")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_create_profile(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/sandbox-profiles",
            json={"name": "python-3.12", "base_image": "img"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_get_profile(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/sandbox-profiles/python-3.12")
    assert resp.status_code == 200
    assert resp.json()["name"] == "python-3.12"


@pytest.mark.asyncio
async def test_update_profile(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/sandbox-profiles/python-3.12",
            json={"name": "python-3.12", "base_image": "img"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"


@pytest.mark.asyncio
async def test_delete_profile(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(
            "/api/v1/sandbox-profiles/python-3.12",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 200


_WEAKENING = [
    {"cap_add": ["ALL"]},
    {"cap_drop": ["NET_RAW"]},
    {"security_opt": ["seccomp=unconfined", "apparmor=unconfined"]},
    {"base_image": "ghcr.io/example/root-image:latest"},
    {"extra_networks": ["bridge"]},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", _WEAKENING)
async def test_create_refuses_a_weakening_profile(app, fields):
    """#553: these fields can undo Ring 1 or the egress drop; nothing is stored."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/sandbox-profiles",
            json={"name": "weak", **fields},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 422, resp.text
    assert [err["loc"][-1] for err in resp.json()["detail"]] == list(fields), resp.json()
    app.state.profiles_repo.upsert.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", _WEAKENING)
async def test_update_refuses_a_weakening_profile(app, fields):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/sandbox-profiles/python-3.12",
            json={"name": "python-3.12", **fields},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    assert resp.status_code == 422, resp.text
    app.state.profiles_repo.upsert.assert_not_awaited()
