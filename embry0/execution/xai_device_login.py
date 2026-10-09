"""RFC 8628 device-code login for an xAI oauth account.

The discovery URL, the public grok device client and its offline scope are
xAI's; the ``slow_down`` interval step is RFC 8628's (section 3.5: five seconds
more). This module does not block, it does not persist a token, and it does
not read a CLI auth file. Both OAuth endpoints must be HTTPS on ``x.ai`` or
``*.x.ai`` before any of them is called.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

AUTH_BASE = "https://auth.x.ai"
DISCOVERY_URL = f"{AUTH_BASE}/.well-known/openid-configuration"
# Public grok device client. Not a secret.
XAI_DEVICE_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_DEVICE_SCOPE = "openid profile email offline_access grok-cli:access api:access"


class XaiDeviceLoginError(Exception):
    """A device-login failure whose ``reason`` is safe to show. The message is that word."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _pin_xai(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "x.ai" or host.endswith(".x.ai")):
        raise XaiDeviceLoginError("endpoint_rejected")


class XaiDeviceLogin:
    """One process-local flow table. ``device_code`` never leaves this object."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client
        self._flows: dict[str, dict[str, Any]] = {}

    async def start(self, client_id: str) -> dict[str, Any]:
        try:
            discovered = await self._http.get(DISCOVERY_URL)
            document = discovered.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise XaiDeviceLoginError("unreachable") from exc
        if not isinstance(document, dict):
            raise XaiDeviceLoginError("endpoint_rejected")
        token_endpoint = str(document.get("token_endpoint") or "")
        device_endpoint = str(document.get("device_authorization_endpoint") or f"{AUTH_BASE}/oauth2/device/code")
        _pin_xai(token_endpoint)
        _pin_xai(device_endpoint)
        try:
            granted = await self._http.post(
                device_endpoint,
                data={"client_id": client_id, "scope": XAI_DEVICE_SCOPE},
            )
            grant = granted.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise XaiDeviceLoginError("unreachable") from exc
        if granted.status_code != 200 or not isinstance(grant, dict) or not grant.get("device_code"):
            raise XaiDeviceLoginError("error")
        device_code = str(grant["device_code"])
        interval = int(grant.get("interval") or 5)
        self._flows[device_code] = {
            "client_id": client_id,
            "token_endpoint": token_endpoint,
            "interval": interval,
        }
        return {
            "device_code": device_code,
            "user_code": str(grant.get("user_code") or ""),
            "verification_uri": str(grant.get("verification_uri") or ""),
            "verification_uri_complete": str(
                grant.get("verification_uri_complete") or grant.get("verification_uri") or ""
            ),
            "interval": interval,
            "expires_in": int(grant.get("expires_in") or 600),
        }

    async def poll(self, device_code: str) -> dict[str, Any]:
        flow = self._flows.get(device_code)
        if flow is None:
            raise XaiDeviceLoginError("unknown")
        try:
            resp = await self._http.post(
                flow["token_endpoint"],
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": flow["client_id"],
                },
            )
        except httpx.HTTPError as exc:
            raise XaiDeviceLoginError("unreachable") from exc
        if resp.status_code == 200:
            return self._success(device_code, flow, resp)
        error = _rfc_error(resp)
        if error == "authorization_pending":
            return {"status": "pending", "interval": flow["interval"]}
        if error == "slow_down":
            flow["interval"] = int(flow["interval"]) + 5
            return {"status": "pending", "interval": flow["interval"]}
        if error in ("expired_token", "access_denied"):
            raise XaiDeviceLoginError(error)
        raise XaiDeviceLoginError("error")

    def _success(self, device_code: str, flow: dict[str, Any], resp: httpx.Response) -> dict[str, str]:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise XaiDeviceLoginError("error") from exc
        refresh = str(payload.get("refresh_token") or "") if isinstance(payload, dict) else ""
        if not refresh:
            raise XaiDeviceLoginError("error")
        self._flows.pop(device_code, None)
        return {
            "refresh_token": refresh,
            "client_id": str(flow["client_id"]),
            "token_endpoint": str(flow["token_endpoint"]),
        }


def _rfc_error(resp: httpx.Response) -> str:
    if resp.status_code != 400:
        return ""
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    return str(body.get("error") or "")
