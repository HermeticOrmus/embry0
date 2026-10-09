"""RFC 8628 device-code login against auth.x.ai. No CLI store is read."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

_DISCOVERY = "https://auth.x.ai/.well-known/openid-configuration"
_DEVICE = "https://auth.x.ai/oauth2/device/code"
_TOKEN = "https://auth.x.ai/oauth2/token"


def _import_login():
    try:
        from embry0.execution.xai_device_login import XaiDeviceLogin, XaiDeviceLoginError
    except ImportError as exc:
        pytest.fail(f"XaiDeviceLogin must be importable: {exc}")
    return XaiDeviceLogin, XaiDeviceLoginError


def _transport(routes: dict[tuple[str, str], httpx.Response]) -> tuple[httpx.AsyncClient, list[tuple[str, str]]]:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        key = (request.method, str(request.url))
        if key not in routes:
            raise AssertionError(f"unexpected {request.method} {request.url}")
        return routes[key]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def _discovery(device: str = _DEVICE, token: str = _TOKEN) -> httpx.Response:
    return httpx.Response(
        200,
        json={"device_authorization_endpoint": device, "token_endpoint": token},
    )


def _device_grant() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "device_code": "device-code-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://auth.x.ai/device",
            "verification_uri_complete": "https://auth.x.ai/device?user_code=ABCD-EFGH",
            "interval": 5,
            "expires_in": 600,
        },
    )


async def test_start_discovers_and_returns_the_user_code():
    XaiDeviceLogin, _error = _import_login()
    client, seen = _transport({("GET", _DISCOVERY): _discovery(), ("POST", _DEVICE): _device_grant()})
    login = XaiDeviceLogin(client)

    started = await login.start("client-abc")

    assert started["device_code"] == "device-code-1"
    assert started["user_code"] == "ABCD-EFGH"
    assert started["verification_uri_complete"] == "https://auth.x.ai/device?user_code=ABCD-EFGH"
    assert started["interval"] == 5
    assert started["expires_in"] == 600
    assert seen == [("GET", _DISCOVERY), ("POST", _DEVICE)]
    await client.aclose()


async def test_start_refuses_an_endpoint_that_is_not_xai():
    XaiDeviceLogin, error = _import_login()
    evil = "https://auth.example/oauth2/token"
    client, seen = _transport({("GET", _DISCOVERY): _discovery(device=evil, token=evil)})
    login = XaiDeviceLogin(client)

    with pytest.raises(error) as raised:
        await login.start("client-abc")

    assert raised.value.reason == "endpoint_rejected"
    assert seen == [("GET", _DISCOVERY)], f"a foreign endpoint must not be called, got {seen}"
    await client.aclose()


async def test_poll_pending_slow_down_and_success():
    XaiDeviceLogin, _error = _import_login()
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _discovery()
        if str(request.url) == _DEVICE:
            return _device_grant()
        form = parse_qs(request.content.decode())
        assert form["grant_type"][0] == "urn:ietf:params:oauth:grant-type:device_code"
        assert form["device_code"][0] == "device-code-1"
        assert form["client_id"][0] == "client-abc"
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        if polls["n"] == 2:
            return httpx.Response(400, json={"error": "slow_down"})
        return httpx.Response(
            200,
            json={
                "refresh_token": "rt-from-device",
                "access_token": "access-must-not-be-returned",
                "expires_in": 900,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    login = XaiDeviceLogin(client)
    await login.start("client-abc")

    pending = await login.poll("device-code-1")
    assert pending == {"status": "pending", "interval": 5}
    slowed = await login.poll("device-code-1")
    assert slowed == {"status": "pending", "interval": 10}
    done = await login.poll("device-code-1")
    assert done["refresh_token"] == "rt-from-device"
    assert done["client_id"] == "client-abc"
    assert done["token_endpoint"] == _TOKEN
    assert "access-must-not-be-returned" not in json.dumps(done)
    await client.aclose()


@pytest.mark.parametrize("reason", ["expired_token", "access_denied"])
async def test_poll_terminal_rfc_error_names_only_the_reason(reason: str):
    XaiDeviceLogin, error = _import_login()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _discovery()
        if str(request.url) == _DEVICE:
            return _device_grant()
        return httpx.Response(400, json={"error": reason, "error_description": "rt-leaked"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    login = XaiDeviceLogin(client)
    await login.start("client-abc")
    with pytest.raises(error) as raised:
        await login.poll("device-code-1")
    assert raised.value.reason == reason
    assert "rt-leaked" not in str(raised.value)
    assert "device-code-1" not in str(raised.value)
    await client.aclose()
