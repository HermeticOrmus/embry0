"""API auth is skipped only under AUTH_DEV_MODE (#518).

WEBHOOK_DEV_MODE skips webhook signature checks and nothing else, and an empty
API_KEY never means open access. Every cell of the matrix is checked against
the HTTP auth and admin dependencies, the jobs WebSocket and the QA SSE stream
with its scoped token, including the cells startup refuses
(test_dev_mode_startup): the guards must hold on their own.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from embry0.api.deps import require_admin, require_auth
from embry0.api.session import SESSION_COOKIE, SessionData, encode_session, fold_tier
from embry0.config import Embry0Config
from tests.unit.api.dev_mode_matrix import KEY, MATRIX, SESSION_SECRET, matrix_config

# ---------------------------------------------------------------------------
# Runtime: HTTP, admin, WebSocket, SSE
# ---------------------------------------------------------------------------


def _runtime_app(config: Embry0Config) -> FastAPI:
    from embry0.api.events.bus import EventBus
    from embry0.api.v1 import qa_events
    from embry0.api.ws import streaming

    app = FastAPI()
    app.state.config = config
    app.state.event_bus = EventBus()
    # No qa_event_bus: a request that passes SSE auth answers 503.

    @app.get("/guarded", dependencies=[require_auth])
    def guarded() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/admin-only", dependencies=[require_admin])
    def admin_only() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(qa_events.router, prefix="/api/v1")
    app.include_router(streaming.router)
    return app


def _ws_opens(client: TestClient, subprotocols: list[str] | None = None) -> bool:
    try:
        with client.websocket_connect("/ws/jobs/job-1/events", subprotocols=subprotocols or []):
            return True
    except WebSocketDisconnect as exc:
        assert exc.code == 4001, f"unexpected close code {exc.code}"
        return False


@pytest.mark.parametrize(("auth_dev", "webhook_dev", "key", "sso"), MATRIX)
def test_http_auth_without_credentials(auth_dev: bool, webhook_dev: bool, key: str, sso: bool) -> None:
    client = TestClient(_runtime_app(matrix_config(auth_dev=auth_dev, webhook_dev=webhook_dev, key=key, sso=sso)))
    status = client.get("/guarded").status_code
    assert status == (200 if auth_dev else 401), f"no credentials -> {status}"


@pytest.mark.parametrize(("auth_dev", "webhook_dev", "key", "sso"), MATRIX)
def test_admin_auth_without_credentials(auth_dev: bool, webhook_dev: bool, key: str, sso: bool) -> None:
    client = TestClient(_runtime_app(matrix_config(auth_dev=auth_dev, webhook_dev=webhook_dev, key=key, sso=sso)))
    status = client.get("/admin-only").status_code
    assert status == (200 if auth_dev else 401), f"no credentials on the admin gate -> {status}"


@pytest.mark.parametrize(("auth_dev", "webhook_dev", "key", "sso"), MATRIX)
def test_ws_without_a_token(auth_dev: bool, webhook_dev: bool, key: str, sso: bool) -> None:
    client = TestClient(_runtime_app(matrix_config(auth_dev=auth_dev, webhook_dev=webhook_dev, key=key, sso=sso)))
    assert _ws_opens(client) is auth_dev, "the WebSocket handshake ignored AUTH_DEV_MODE"


@pytest.mark.parametrize(("auth_dev", "webhook_dev", "key", "sso"), MATRIX)
def test_sse_without_a_token(auth_dev: bool, webhook_dev: bool, key: str, sso: bool) -> None:
    client = TestClient(_runtime_app(matrix_config(auth_dev=auth_dev, webhook_dev=webhook_dev, key=key, sso=sso)))
    status = client.get("/api/v1/qa/runs/run-1/events").status_code
    assert status == (503 if auth_dev else 401), f"no credentials on the SSE stream -> {status}"


@pytest.mark.parametrize(("auth_dev", "webhook_dev", "key", "sso"), MATRIX)
def test_scoped_token(auth_dev: bool, webhook_dev: bool, key: str, sso: bool) -> None:
    """A scoped token opens the SSE stream and the WebSocket only when it was
    signed with a secret. With no API_KEY and no SESSION_SECRET the signing
    material is empty, so anyone could mint one; those must be refused."""
    from embry0.api.scoped_tokens import mint_scoped_token, signing_material

    config = matrix_config(auth_dev=auth_dev, webhook_dev=webhook_dev, key=key, sso=sso)
    client = TestClient(_runtime_app(config))
    material = signing_material(config)
    accepted = auth_dev or bool(material)

    sse_token, _ = mint_scoped_token(material, "sse:run-1")
    status = client.get("/api/v1/qa/runs/run-1/events", params={"token": sse_token}).status_code
    assert status == (503 if accepted else 401), f"SSE token signed with {material!r:.8} -> {status}"

    ws_token, _ = mint_scoped_token(material, "ws:job-1")
    opened = _ws_opens(client, [f"embry0.token.{ws_token}"])
    assert opened is accepted, f"WS token signed with {material!r:.8}: opened={opened}"


@pytest.mark.parametrize(("auth_dev", "webhook_dev", "key", "sso"), [p for p in MATRIX if p.values[2]])
def test_master_key_passes(auth_dev: bool, webhook_dev: bool, key: str, sso: bool) -> None:
    client = TestClient(_runtime_app(matrix_config(auth_dev=auth_dev, webhook_dev=webhook_dev, key=key, sso=sso)))
    bearer = {"Authorization": f"Bearer {KEY}"}
    assert client.get("/guarded", headers=bearer).status_code == 200
    assert client.get("/admin-only", headers=bearer).status_code == 200


@pytest.mark.parametrize(("auth_dev", "webhook_dev", "key", "sso"), [p for p in MATRIX if not p.values[2]])
def test_an_empty_bearer_never_matches_an_empty_api_key(auth_dev: bool, webhook_dev: bool, key: str, sso: bool) -> None:
    client = TestClient(_runtime_app(matrix_config(auth_dev=auth_dev, webhook_dev=webhook_dev, key=key, sso=sso)))
    expected = 200 if auth_dev else 401
    for path in ("/guarded", "/admin-only"):
        status = client.get(path, headers={"Authorization": "Bearer "}).status_code
        assert status == expected, f"{path} with an empty bearer -> {status}"


@pytest.mark.parametrize(("auth_dev", "webhook_dev", "key", "sso"), [p for p in MATRIX if p.values[3]])
def test_sso_session_tiers(auth_dev: bool, webhook_dev: bool, key: str, sso: bool) -> None:
    """With SSO on, an operator session passes the auth gate but not the admin
    gate, and an admin session passes both, whatever API_KEY holds."""
    client = TestClient(_runtime_app(matrix_config(auth_dev=auth_dev, webhook_dev=webhook_dev, key=key, sso=sso)))

    def cookie(role: str) -> dict[str, str]:
        roles = [role]
        session = SessionData(sub="auth0|u1", email="u@example.com", name="U", roles=roles, tier=fold_tier(roles))
        return {SESSION_COOKIE: encode_session(SESSION_SECRET, session)}

    client.cookies.update(cookie("embry0-operator"))
    assert client.get("/guarded").status_code == 200
    assert client.get("/admin-only").status_code == (200 if auth_dev else 403), "an operator passed the admin gate"
    client.cookies.update(cookie("embry0-admin"))
    assert client.get("/admin-only").status_code == 200


# ---------------------------------------------------------------------------
# Fallbacks behind the gate
# ---------------------------------------------------------------------------


def _unauthenticated_request(config: Embry0Config) -> Request:
    """A request that reached a handler with no session and no principal."""
    app = FastAPI()
    app.state.config = config
    return Request({"type": "http", "app": app, "method": "GET", "path": "/", "headers": [], "query_string": b""})


@pytest.mark.parametrize("key", [pytest.param("", id="key=empty"), pytest.param(KEY, id="key=set")])
@pytest.mark.parametrize(
    "webhook_dev", [pytest.param(False, id="webhook_dev=0"), pytest.param(True, id="webhook_dev=1")]
)
async def test_api_keys_caller_grants_admin_only_in_auth_dev_mode(webhook_dev: bool, key: str) -> None:
    from embry0.api.session import TIER_ADMIN
    from embry0.api.v1.api_keys import _caller

    assert await _caller(_unauthenticated_request(matrix_config(auth_dev=True, webhook_dev=webhook_dev, key=key))) == (
        TIER_ADMIN,
        None,
    )
    try:
        tier = await _caller(_unauthenticated_request(matrix_config(webhook_dev=webhook_dev, key=key)))
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        pytest.fail(f"an unauthenticated caller outside AUTH_DEV_MODE got tier {tier!r}")


@pytest.mark.parametrize("key", [pytest.param("", id="key=empty"), pytest.param(KEY, id="key=set")])
@pytest.mark.parametrize(
    "webhook_dev", [pytest.param(False, id="webhook_dev=0"), pytest.param(True, id="webhook_dev=1")]
)
async def test_audit_caller_scope_grants_admin_only_in_auth_dev_mode(webhook_dev: bool, key: str) -> None:
    from embry0.api.session import TIER_ADMIN
    from embry0.api.v1.audit import _caller_scope

    scope = await _caller_scope(
        _unauthenticated_request(matrix_config(auth_dev=True, webhook_dev=webhook_dev, key=key))
    )
    assert scope == (TIER_ADMIN, [])
    try:
        scope = await _caller_scope(_unauthenticated_request(matrix_config(webhook_dev=webhook_dev, key=key)))
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        pytest.fail(f"an unauthenticated caller outside AUTH_DEV_MODE got scope {scope!r}")


def test_a_scoped_token_signed_with_no_secret_never_verifies() -> None:
    from embry0.api.scoped_tokens import mint_scoped_token, verify_scoped_token

    token, _ = mint_scoped_token("", "ws:job-1")
    assert not verify_scoped_token("", "ws:job-1", token), "a token signed with empty material verified"
