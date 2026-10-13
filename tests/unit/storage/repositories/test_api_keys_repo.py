"""ApiKeysRepository unit tests (RAV-1056 Phase B) — mocked DB."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.storage.repositories.api_keys import ApiKeysRepository, hash_token


def _fake_row(**overrides):
    base = {
        "id": "key-abc123def456",
        "user_id": None,
        "name": "k",
        "prefix": "emb_xxxxxxxx",
        "scope": "read",
        "created_by": "tester",
        "created_at": None,
        "expires_at": None,
        "last_used_at": None,
        "revoked_at": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_create_returns_plaintext_and_stores_hash():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=_fake_row())
    repo = ApiKeysRepository(db)
    row, plaintext = await repo.create(name="k", scope="read", user_id=None, created_by="tester")
    assert plaintext.startswith("emb_") and len(plaintext) > 20
    args = db.fetchrow.call_args.args
    assert "INSERT INTO api_keys" in args[0]
    assert args[4] == hash_token(plaintext)  # key_hash bound
    assert args[5] == plaintext[:12]  # prefix bound
    assert row.id.startswith("key-")


@pytest.mark.asyncio
async def test_create_rejects_bad_scope():
    repo = ApiKeysRepository(MagicMock())
    with pytest.raises(ValueError):
        await repo.create(name="k", scope="root", user_id=None, created_by="t")


@pytest.mark.asyncio
async def test_rotate_service_key_updates_by_name():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={"id": "key-svc111111"})
    repo = ApiKeysRepository(db)
    token = await repo.rotate_service_key("svc-context-proxy", "read")
    assert token.startswith("emb_")
    sql = db.fetchrow.call_args_list[0].args[0]
    assert "UPDATE api_keys" in sql and "user_id IS NULL" in sql


@pytest.mark.asyncio
async def test_rotate_service_key_inserts_when_missing():
    db = MagicMock()
    db.fetchrow = AsyncMock(side_effect=[None, {"id": "key-new"}])
    repo = ApiKeysRepository(db)
    await repo.rotate_service_key("svc-context-proxy", "read")
    assert "INSERT INTO api_keys" in db.fetchrow.call_args_list[1].args[0]


@pytest.mark.asyncio
async def test_touch_last_used_is_throttled_in_sql():
    db = MagicMock()
    db.execute = AsyncMock()
    repo = ApiKeysRepository(db)
    await repo.touch_last_used("key-x")
    sql = db.execute.call_args.args[0]
    assert "last_used_at IS NULL OR last_used_at <" in sql


@pytest.mark.asyncio
async def test_revoke_only_once():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=None)
    repo = ApiKeysRepository(db)
    assert await repo.revoke("key-x") is None
    assert "revoked_at IS NULL" in db.fetchrow.call_args.args[0]


@pytest.mark.asyncio
async def test_create_binds_the_allowlist_and_stores_empty_as_null():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=_fake_row(allowed_repos=["acme/widgets"]))
    repo = ApiKeysRepository(db)
    row, _ = await repo.create(name="k", scope="read", user_id=None, created_by="t", allowed_repos=["acme/widgets"])
    assert "allowed_repos" in db.fetchrow.call_args.args[0]
    assert db.fetchrow.call_args.args[9] == ["acme/widgets"]
    assert row.allowed_repos == ("acme/widgets",)

    await repo.create(name="k", scope="read", user_id=None, created_by="t", allowed_repos=[])
    assert db.fetchrow.call_args.args[9] is None, "an empty list must be stored as NULL (unrestricted)"


@pytest.mark.asyncio
async def test_a_row_without_the_column_is_unrestricted():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=_fake_row())
    row = await ApiKeysRepository(db).get("key-abc123def456")
    assert row is not None and row.allowed_repos is None


@pytest.mark.asyncio
async def test_set_allowed_repos_updates_and_stores_empty_as_null():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=_fake_row(allowed_repos=None))
    repo = ApiKeysRepository(db)
    await repo.set_allowed_repos("key-x", [])
    sql, key_id, value = db.fetchrow.call_args.args
    assert "UPDATE api_keys SET allowed_repos" in sql and key_id == "key-x" and value is None
