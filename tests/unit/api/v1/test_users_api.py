"""User-management endpoint tests (RAV-1056 Phase A)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from embry0.api.deps import bust_user_gate_cache, require_admin
from embry0.api.session import SESSION_COOKIE, SessionData, encode_session
from embry0.config import Embry0Config
from embry0.storage.repositories.users import UserRow

SECRET = "s" * 40


def _config() -> Embry0Config:
    with patch.dict(os.environ, {}, clear=True):
        return Embry0Config(
            _env_file=None,
            auth_dev_mode=False,
            api_key="master-key",
            auth0_issuer="https://tenant.example.com/",
            auth0_client_id="cid",
            auth0_client_secret="sec",
            session_secret=SECRET,
            sso_public_origin="https://embry0.example.com",
            # #393: SSO is on only with a roles claim, so a session-auth fixture states one.
            sso_roles_claim="https://example.com/roles",
        )


def _user_row(**overrides) -> UserRow:
    base = dict(
        id="usr-abc123def456",
        auth0_sub="waad|X|1",
        email="t@example.com",
        name="T",
        tier="admin",
        disabled=False,
        session_epoch=1,
        login_count=2,
        first_login_at=None,
        last_login_at=None,
        created_at=None,
    )
    base.update(overrides)
    return UserRow(**base)


def _app() -> FastAPI:
    from embry0.api.v1 import users

    app = FastAPI()
    app.state.config = _config()
    app.state.db = None
    repo = MagicMock()
    repo.list_users = AsyncMock(return_value=[_user_row()])
    repo.count = AsyncMock(return_value=1)
    repo.set_disabled = AsyncMock(return_value=_user_row(disabled=True))
    repo.bump_epoch = AsyncMock(return_value=_user_row(session_epoch=2))
    # the admin session gate consults get_by_sub for the CALLING admin
    gate_row = MagicMock()
    gate_row.session_epoch = 1
    gate_row.disabled = False
    repo.get_by_sub = AsyncMock(return_value=gate_row)
    app.state.users_repo = repo
    app.include_router(users.router, prefix="/api/v1", dependencies=[require_admin])
    return app


def _admin_cookie() -> str:
    return encode_session(
        SECRET,
        SessionData(sub="auth0|adm", email="adm@example.com", name="A", roles=["embry0-admin"], tier="admin", epoch=1),
    )


def _operator_cookie() -> str:
    return encode_session(
        SECRET,
        SessionData(
            sub="auth0|op", email="op@example.com", name="O", roles=["embry0-operator"], tier="operator", epoch=1
        ),
    )


def setup_function() -> None:
    bust_user_gate_cache()


def test_list_users_admin_only():
    app = _app()
    client = TestClient(app)
    assert client.get("/api/v1/users").status_code == 401
    client.cookies.set(SESSION_COOKIE, _operator_cookie())
    assert client.get("/api/v1/users").status_code == 403
    body = client.get("/api/v1/users", headers={"Authorization": "Bearer master-key"}).json()
    assert body["total"] == 1 and body["users"][0]["email"] == "t@example.com"


def test_admin_session_can_list():
    app = _app()
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, _admin_cookie())
    assert client.get("/api/v1/users").status_code == 200


def test_disable_busts_gate_cache_and_audits():
    app = _app()
    client = TestClient(app)
    resp = client.put(
        "/api/v1/users/usr-abc123def456/disabled",
        json={"disabled": True},
        headers={"Authorization": "Bearer master-key", "X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200 and resp.json()["disabled"] is True
    app.state.users_repo.set_disabled.assert_awaited_once_with("usr-abc123def456", True)


def test_force_logout_bumps_epoch():
    app = _app()
    client = TestClient(app)
    resp = client.post(
        "/api/v1/users/usr-abc123def456/force-logout",
        headers={"Authorization": "Bearer master-key", "X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200 and resp.json()["session_epoch"] == 2


def test_unknown_user_404():
    app = _app()
    app.state.users_repo.bump_epoch = AsyncMock(return_value=None)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/users/usr-nope/force-logout",
        headers={"Authorization": "Bearer master-key", "X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 404
