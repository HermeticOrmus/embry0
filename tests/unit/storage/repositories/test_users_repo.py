"""UsersRepository unit tests (RAV-1056 Phase A) — mocked DB, bound-args
assertions (the house pattern for storage code whose requires_postgres
round-trips skip in the local gate)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.storage.repositories.users import UsersRepository


def _fake_row(**overrides):
    base = {
        "id": "usr-abc123def456",
        "auth0_sub": "waad|X|1",
        "email": "u@example.com",
        "name": "U",
        "tier": "operator",
        "disabled": False,
        "session_epoch": 1,
        "login_count": 3,
        "first_login_at": None,
        "last_login_at": None,
        "created_at": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_upsert_login_updates_existing_by_sub():
    db = MagicMock()
    db.fetchrow = AsyncMock(side_effect=[{"id": "usr-existing0001"}, _fake_row(id="usr-existing0001")])
    repo = UsersRepository(db)
    row = await repo.upsert_login(auth0_sub="waad|X|1", email="u@example.com", name="U", tier="operator")
    assert row.id == "usr-existing0001"
    update_call = db.fetchrow.call_args_list[1]
    assert "UPDATE users" in update_call.args[0]
    assert update_call.args[1] == "usr-existing0001"
    assert update_call.args[2:] == ("waad|X|1", "u@example.com", "U", "operator")


@pytest.mark.asyncio
async def test_upsert_login_inserts_new_user():
    db = MagicMock()
    db.fetchrow = AsyncMock(side_effect=[None, _fake_row(login_count=1)])
    repo = UsersRepository(db)
    row = await repo.upsert_login(auth0_sub="waad|X|1", email="u@example.com", name="U", tier="admin")
    insert_call = db.fetchrow.call_args_list[1]
    assert "INSERT INTO users" in insert_call.args[0]
    assert insert_call.args[1].startswith("usr-")
    assert insert_call.args[2:] == ("waad|X|1", "u@example.com", "U", "admin")
    assert row.login_count == 1


@pytest.mark.asyncio
async def test_upsert_login_rejects_bad_tier():
    repo = UsersRepository(MagicMock())
    with pytest.raises(ValueError):
        await repo.upsert_login(auth0_sub="s", email="e", name="n", tier="root")


@pytest.mark.asyncio
async def test_bump_epoch_increments():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=_fake_row(session_epoch=4))
    repo = UsersRepository(db)
    row = await repo.bump_epoch("usr-x")
    assert row is not None and row.session_epoch == 4
    assert "session_epoch = session_epoch + 1" in db.fetchrow.call_args.args[0]


@pytest.mark.asyncio
async def test_set_disabled_binds_flag():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=_fake_row(disabled=True))
    repo = UsersRepository(db)
    row = await repo.set_disabled("usr-x", True)
    assert row is not None and row.disabled is True
    assert db.fetchrow.call_args.args[1:] == ("usr-x", True)
