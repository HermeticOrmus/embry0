"""POST /api/v1/repos/bootstrap endpoint tests (EMB-49)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_bootstrap_rejects_bad_repo_shape(api_client) -> None:
    r = await api_client.post("/api/v1/repos/bootstrap", json={"repo": "not-a-repo"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_bootstrap_rejects_bad_app_slug(api_client) -> None:
    r = await api_client.post("/api/v1/repos/bootstrap", json={"repo": "a/b", "app": "Bad App"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_bootstrap_rejects_unknown_field(api_client) -> None:
    r = await api_client.post("/api/v1/repos/bootstrap", json={"repo": "a/b", "nope": 1})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_bootstrap_happy_path_writes_db(api_client, monkeypatch) -> None:
    """RAV-1251: bootstrap writes the DB config + registers the repo."""

    async def _fake_verify(repo, token, credential_resolver=None):
        return "main", True

    monkeypatch.setattr("embry0.services.bootstrap._verify_github_repo", _fake_verify)
    r = await api_client.post("/api/v1/repos/bootstrap", json={"repo": "acme/fresh"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["qa_required"] == "never"
    assert body["qa_config_path"] == "db:repo_qa_configs/acme/fresh"
    # The qa-config GET serves the stored document.
    r2 = await api_client.get("/api/v1/repos/acme/fresh/qa-config")
    assert r2.status_code == 200
    assert "version: 2" in r2.text
    # And the repo is registered.
    r3 = await api_client.get("/api/v1/repos")
    repos = {row["repo"]: row for row in r3.json()}
    assert repos["acme/fresh"]["has_qa_config"] is True


@pytest.mark.asyncio
async def test_bootstrap_conflict_maps_to_400(api_client, monkeypatch) -> None:
    async def _fake_verify(repo, token, credential_resolver=None):
        return "main", True

    monkeypatch.setattr("embry0.services.bootstrap._verify_github_repo", _fake_verify)
    assert (await api_client.post("/api/v1/repos/bootstrap", json={"repo": "acme/fresh"})).status_code == 200
    r = await api_client.post("/api/v1/repos/bootstrap", json={"repo": "acme/fresh"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]
