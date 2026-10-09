"""Hosted MCP endpoint (embry0_mcp.http) — bearer gate + per-request forwarding."""

from __future__ import annotations

import httpx
import pytest

from embry0_mcp.client import Embry0Client, caller_bearer
from embry0_mcp.http import BearerForwardMiddleware, build_app, transport_security


async def _call(app, headers: list[tuple[bytes, bytes]]) -> tuple[int, bytes]:
    """Drive one ASGI http request; return (status, body)."""
    sent: dict[str, object] = {}
    body = b""

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal body
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers, "query_string": b""}
    await app(scope, receive, send)
    return int(sent["status"]), body


@pytest.mark.asyncio
async def test_anonymous_request_is_rejected_at_the_door():
    """The tool surface must not be enumerable without a key."""
    inner_called = False

    async def inner(scope, receive, send):
        nonlocal inner_called
        inner_called = True

    status, body = await _call(BearerForwardMiddleware(inner), headers=[])
    assert status == 401
    assert b"Authorization" in body
    assert inner_called is False


@pytest.mark.asyncio
async def test_empty_bearer_is_rejected():
    status, _ = await _call(BearerForwardMiddleware(None), headers=[(b"authorization", b"Bearer   ")])
    assert status == 401


@pytest.mark.asyncio
async def test_bearer_is_stashed_for_the_request_and_reset_after():
    seen: list[str | None] = []

    async def inner(scope, receive, send):
        seen.append(caller_bearer.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    status, _ = await _call(BearerForwardMiddleware(inner), headers=[(b"authorization", b"Bearer emb_caller-key")])
    assert status == 200
    assert seen == ["emb_caller-key"]
    assert caller_bearer.get() is None  # reset — no bleed across requests


@pytest.mark.asyncio
async def test_client_forwards_the_caller_bearer_over_the_env_key():
    """The orchestrator must see the CALLER's key, never a service-wide one."""
    captured: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={})

    client = Embry0Client("http://x", api_key="service-env-key", transport=httpx.MockTransport(handler))
    token = caller_bearer.set("emb_caller-key")
    try:
        await client._request("GET", "/api/v1/queue")
    finally:
        caller_bearer.reset(token)
        await client.aclose()
    assert captured == ["Bearer emb_caller-key"]


@pytest.mark.asyncio
async def test_client_falls_back_to_the_env_key_without_a_caller():
    captured: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={})

    client = Embry0Client("http://x", api_key="service-env-key", transport=httpx.MockTransport(handler))
    try:
        await client._request("GET", "/api/v1/queue")
    finally:
        await client.aclose()
    assert captured == ["Bearer service-env-key"]


def test_app_builds_stateless_on_the_mcp_path():
    app = build_app()
    assert isinstance(app, BearerForwardMiddleware)


def test_app_disables_host_validation_unless_allowlisted(monkeypatch):
    """Behind nginx on arbitrary hostnames the SDK's localhost-only Host
    check 421s every request; rebinding doesn't apply to a Bearer-gated
    API. MCP_ALLOWED_HOSTS re-enables it with an explicit list. What each
    policy does to a request is pinned in test_http_wire.py."""
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    assert transport_security().enable_dns_rebinding_protection is False

    monkeypatch.setenv("MCP_ALLOWED_HOSTS", " acme.embry0.ai , ,other.embry0.ai:* ")
    ts = transport_security()
    assert ts.enable_dns_rebinding_protection is True
    assert ts.allowed_hosts == ["acme.embry0.ai", "other.embry0.ai:*"]
    assert ts.allowed_origins == []
