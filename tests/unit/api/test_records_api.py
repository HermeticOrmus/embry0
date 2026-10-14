"""Repo records endpoints against Postgres: shapes, status codes, nested repos."""

from __future__ import annotations

import pytest

from embry0.storage.repositories.repo_records import MAX_BODY_BYTES

pytestmark = pytest.mark.requires_postgres


async def _register(api_client, repo: str) -> None:
    r = await api_client.post("/api/v1/repos", json={"repo": repo, "forge": "gitlab"})
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_put_creates_then_is_idempotent(api_client) -> None:
    await _register(api_client, "acme/widgets")
    url = "/api/v1/repos/acme/widgets/records/outage"
    first = await api_client.put(url, json={"kind": "rca", "body": "# Outage\n\nIt broke."})
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True and first.json()["revision"] == 1
    assert first.json()["title"] == "Outage"
    assert first.json()["author"], "the writer must be recorded"

    again = await api_client.put(url, json={"kind": "rca", "body": "# Outage\n\nIt broke."})
    assert again.status_code == 200, again.text
    assert again.json()["changed"] is False and again.json()["current_revision"] == 1

    second = await api_client.put(url, json={"kind": "rca", "body": "# Outage\n\nIt broke twice."})
    assert second.json()["changed"] is True and second.json()["current_revision"] == 2


@pytest.mark.asyncio
async def test_get_list_and_revisions_on_a_nested_repo(api_client) -> None:
    repo = "group/sub/project"
    await _register(api_client, repo)
    base = f"/api/v1/repos/{repo}/records"
    await api_client.put(f"{base}/restart", json={"kind": "runbook", "body": "v1", "title": "Restart"})
    await api_client.put(f"{base}/restart", json={"kind": "runbook", "body": "v2", "title": "Restart"})
    await api_client.put(f"{base}/notes", json={"kind": "note", "body": "# Notes\n"})

    listing = await api_client.get(base)
    assert listing.status_code == 200, listing.text
    assert listing.json()["repo"] == repo
    assert {r["slug"] for r in listing.json()["records"]} == {"restart", "notes"}
    assert {r["slug"] for r in (await api_client.get(base, params={"kind": "runbook"})).json()["records"]} == {
        "restart"
    }
    assert {r["slug"] for r in (await api_client.get(base, params={"q": "notes"})).json()["records"]} == {"notes"}

    current = (await api_client.get(f"{base}/restart")).json()
    assert current["body"] == "v2" and current["revision"] == 2 and current["repo"] == repo
    old = await api_client.get(f"{base}/restart", params={"revision": 1})
    assert old.status_code == 200 and old.json()["body"] == "v1"
    assert (await api_client.get(f"{base}/restart", params={"revision": 9})).status_code == 404

    revisions = await api_client.get(f"{base}/restart/revisions")
    assert revisions.status_code == 200
    assert [r["revision"] for r in revisions.json()["revisions"]] == [2, 1]
    assert "body" not in revisions.json()["revisions"][0], "the revision list must not carry bodies"


@pytest.mark.asyncio
async def test_a_slug_named_like_a_route_suffix(api_client) -> None:
    """``revisions`` as a slug must address the record, not the revisions route."""
    await _register(api_client, "acme/widgets")
    r = await api_client.put("/api/v1/repos/acme/widgets/records/revisions", json={"kind": "note", "body": "x"})
    assert r.status_code == 201, r.text
    assert (await api_client.get("/api/v1/repos/acme/widgets/records/revisions")).json()["slug"] == "revisions"


@pytest.mark.asyncio
async def test_error_status_codes(api_client) -> None:
    await _register(api_client, "acme/widgets")
    base = "/api/v1/repos/acme/widgets/records"
    assert (await api_client.get("/api/v1/repos/acme/ghost/records")).status_code == 404
    assert (
        await api_client.put("/api/v1/repos/acme/ghost/records/a", json={"kind": "note", "body": "x"})
    ).status_code == 404
    assert (await api_client.get(f"{base}/missing")).status_code == 404
    assert (await api_client.get(f"{base}/missing/revisions")).status_code == 404
    assert (await api_client.put(f"{base}/Bad", json={"kind": "note", "body": "x"})).status_code == 422
    assert (await api_client.put(f"{base}/a", json={"kind": "diary", "body": "x"})).status_code == 422
    assert (await api_client.put(f"{base}/a", json={"kind": "note", "body": "x", "extra": 1})).status_code == 422
    assert (await api_client.get(base, params={"kind": "diary"})).status_code == 422
    too_big = await api_client.put(f"{base}/a", json={"kind": "note", "body": "x" * (MAX_BODY_BYTES + 1)})
    assert too_big.status_code == 413, too_big.text
    assert "512 KiB" in too_big.json()["detail"]
    await api_client.put(f"{base}/a", json={"kind": "note", "body": "x"})
    conflict = await api_client.put(f"{base}/a", json={"kind": "rca", "body": "x"})
    assert conflict.status_code == 409 and "fixed" in conflict.json()["detail"]


@pytest.mark.asyncio
async def test_delete_record_then_repo(api_client) -> None:
    await _register(api_client, "acme/widgets")
    await api_client.put("/api/v1/repos/acme/widgets/records/a", json={"kind": "note", "body": "x"})
    refused = await api_client.delete("/api/v1/repos/acme/widgets")
    assert refused.status_code == 409, "a repo with records must not be deletable"
    assert "1 record" in refused.json()["detail"]
    assert (await api_client.delete("/api/v1/repos/acme/widgets/records/a")).status_code == 204
    assert (await api_client.delete("/api/v1/repos/acme/widgets/records/a")).status_code == 404
    assert (await api_client.delete("/api/v1/repos/acme/widgets")).status_code == 204
