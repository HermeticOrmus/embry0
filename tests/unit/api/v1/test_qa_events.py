"""Route tests for the SSE liveness endpoint.

Endpoint: GET /api/v1/qa/runs/{run_id}/events

Coverage:
  1. Returns 503 when app.state.qa_event_bus is missing.
  2. Streams JSON-encoded events in publish order, then closes after a
     'done' event.
  3. Requires auth: 401 without credentials; accepts either the Bearer
     header or the ``token`` query parameter (RAV-1058 A1 — EventSource
     cannot send headers).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from embry0.qa.event_bus import QAEventBus

_KEY = "test-api-key-32-characters-minimum-x"
_AUTH = {"Authorization": f"Bearer {_KEY}"}


def _make_app(*, with_bus: bool = True):
    """Build a bare FastAPI app without entering the lifespan, mirroring
    the pattern from ``test_qa_dashboard_routes.py``. ``app.state`` is
    populated manually so each test can control the bus presence."""
    from embry0.api.app import create_app

    app = create_app()

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.api_key = _KEY
    cfg.auth_dev_mode = False
    cfg.webhook_dev_mode = False
    app.state.config = cfg

    if with_bus:
        app.state.qa_event_bus = QAEventBus()
    elif hasattr(app.state, "qa_event_bus"):
        # The lifespan never ran in this test path, but defensively clear
        # any attribute that might have been populated by create_app().
        delattr(app.state, "qa_event_bus")

    return app, TestClient(app)


# ---------------------------------------------------------------------------
# Test 1: 503 when bus is missing
# ---------------------------------------------------------------------------


def test_sse_route_returns_503_when_bus_missing() -> None:
    _, client = _make_app(with_bus=False)
    resp = client.get("/api/v1/qa/runs/run-1/events", headers=_AUTH)
    assert resp.status_code == 503
    assert "qa event bus" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test 2: streams events until 'done', then closes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_route_streams_events_until_done() -> None:
    """End-to-end: publish two subtask_status + a done; the route yields
    them in order and the response body terminates after 'done'."""
    app, _ = _make_app(with_bus=True)
    bus: QAEventBus = app.state.qa_event_bus

    # We invoke the route handler directly with a stub Request rather than
    # spinning up TestClient — sync TestClient blocks the event loop while
    # consuming the streamed body, which would prevent the publishing
    # coroutine below from running. Direct invocation lets us schedule
    # publishes between iterator advances on the same loop.
    from starlette.requests import Request

    from embry0.api.v1.qa_events import stream_run_events

    # Minimal ASGI scope; ``is_disconnected()`` reads from ``receive``.
    async def fake_receive():
        # Block forever — the route uses ``request.is_disconnected()`` which
        # checks the receive channel. Returning a "lifespan" type would
        # confuse it; the simplest correct stub is to never resolve.
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}  # pragma: no cover

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/qa/runs/run-events-1/events",
        "headers": [],
        "app": app,
    }
    request = Request(scope, fake_receive)
    response = await stream_run_events(run_id="run-events-1", request=request)

    body_iter = response.body_iterator

    async def publish_after_subscribe() -> None:
        # Yield once so the route's async-for has registered its queue
        # before we publish — otherwise the publish would land before the
        # subscriber and be dropped (no replay buffer).
        await asyncio.sleep(0.05)
        await bus.publish(
            "run-events-1",
            {"type": "subtask_status", "app": "hub", "status": "passed"},
        )
        await bus.publish(
            "run-events-1",
            {"type": "subtask_status", "app": "companion", "status": "failed"},
        )
        await bus.publish(
            "run-events-1",
            {"type": "done", "run_id": "run-events-1", "overall_status": "failed"},
        )

    pub_task = asyncio.create_task(publish_after_subscribe())

    # sse-starlette's EventSourceResponse.body_iterator yields the dicts the
    # route's async-generator yields (each ``{"data": "..."}``) — not the
    # ASGI bytes those eventually serialise to. The route appends a single
    # ``data`` key per event, so we just JSON-decode each one.
    chunks: list[dict] = []
    try:
        async for chunk in body_iter:
            chunks.append(chunk)
    finally:
        await pub_task

    events = [json.loads(c["data"]) for c in chunks if "data" in c]
    types = [e.get("type") for e in events]
    assert types == ["subtask_status", "subtask_status", "done"]
    assert events[0]["app"] == "hub"
    assert events[1]["app"] == "companion"
    assert events[2]["overall_status"] == "failed"


# ---------------------------------------------------------------------------
# Test 3: auth gate
# ---------------------------------------------------------------------------


def test_sse_route_requires_auth() -> None:
    _, client = _make_app(with_bus=True)
    # No credentials at all — 401 (not 503), proving the auth dependency
    # runs before the bus check.
    resp = client.get("/api/v1/qa/runs/run-1/events")
    assert resp.status_code == 401


def test_sse_route_rejects_wrong_query_token() -> None:
    _, client = _make_app(with_bus=True)
    resp = client.get("/api/v1/qa/runs/run-1/events", params={"token": "wrong-key"})
    assert resp.status_code == 401


def test_sse_route_rejects_master_key_in_query() -> None:
    """RAV-1060: the master API key must NOT be accepted via the URL — a
    query-borne master key lands in access logs, browser history, and
    Referer. Only run-scoped tokens ride the query string now."""
    _, client = _make_app(with_bus=True)
    resp = client.get("/api/v1/qa/runs/run-1/events", params={"token": _KEY})
    assert resp.status_code == 401


def test_sse_route_still_accepts_bearer_header() -> None:
    _, client = _make_app(with_bus=False)
    resp = client.get("/api/v1/qa/runs/run-1/events", headers=_AUTH)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Test 4: run-scoped SSE tokens (RAV-1060)
# ---------------------------------------------------------------------------


def test_events_token_route_requires_auth() -> None:
    _, client = _make_app(with_bus=True)
    resp = client.post("/api/v1/qa/runs/run-1/events-token", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 401


def test_events_token_flow_end_to_end() -> None:
    """POST the token route with Bearer auth, then open the stream with the
    scoped token — proven past auth by reaching the 503 bus check."""
    _, client = _make_app(with_bus=False)
    minted = client.post(
        "/api/v1/qa/runs/run-9/events-token",
        headers={**_AUTH, "X-Requested-With": "XMLHttpRequest"},
    )
    assert minted.status_code == 200
    token = minted.json()["token"]
    assert minted.json()["expires_in"] > 0
    assert _KEY not in token  # the credential in the URL is never the master key

    resp = client.get("/api/v1/qa/runs/run-9/events", params={"token": token})
    assert resp.status_code == 503  # past auth, into the bus check


def test_scoped_token_is_bound_to_its_run() -> None:
    _, client = _make_app(with_bus=True)
    minted = client.post(
        "/api/v1/qa/runs/run-a/events-token",
        headers={**_AUTH, "X-Requested-With": "XMLHttpRequest"},
    )
    token = minted.json()["token"]
    resp = client.get("/api/v1/qa/runs/run-b/events", params={"token": token})
    assert resp.status_code == 401


def test_scoped_token_expires() -> None:
    from embry0.api.scoped_tokens import DEFAULT_TTL_SECONDS as _SSE_TOKEN_TTL_SECONDS
    from embry0.api.v1.qa_events import mint_sse_token, verify_sse_token

    token, _ = mint_sse_token(_KEY, "run-x", now=1_000_000.0)
    assert verify_sse_token(_KEY, "run-x", token, now=1_000_000.0 + 10)
    assert not verify_sse_token(_KEY, "run-x", token, now=1_000_000.0 + _SSE_TOKEN_TTL_SECONDS + 1)


def test_scoped_token_rejects_tampering() -> None:
    from embry0.api.v1.qa_events import mint_sse_token, verify_sse_token

    token, _ = mint_sse_token(_KEY, "run-x", now=1_000_000.0)
    version, exp, sig = token.split(".")
    assert not verify_sse_token(_KEY, "run-x", f"{version}.{int(exp) + 9999}.{sig}", now=1_000_000.0)
    assert not verify_sse_token(_KEY, "run-x", f"{version}.{exp}.{'0' * len(sig)}", now=1_000_000.0)
    assert not verify_sse_token("other-key", "run-x", token, now=1_000_000.0)
    assert not verify_sse_token(_KEY, "run-x", "garbage", now=1_000_000.0)
