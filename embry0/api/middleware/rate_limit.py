"""Per-principal API rate limiting middleware (RAV-1056 Phase C).

All public traffic arrives via the Cloudflare tunnel from effectively one
peer address, so IP-keyed buckets collapse every authenticated caller into
a single shared window. Buckets key on the resolved credential instead:
``master`` / ``key:<key_id>`` / ``user:<email>``, falling back to
``ip:<addr>`` for anonymous or invalid credentials — invalid tokens
deliberately SHARE the IP bucket rather than minting per-token buckets.

Webhook endpoints (GitHub + Linear) are NOT exempt anymore (RAV-1070): they
are unauthenticated-until-HMAC and read the full body before verification,
so they get their own IP-keyed throttle with a separate, more generous
ceiling — ``request.client.host`` is the real peer thanks to uvicorn's
``--proxy-headers`` + ``FORWARDED_ALLOW_IPS`` (RAV-1060), so one abusive
source can no longer hammer the HMAC path for free without also collapsing
legitimate GitHub/Linear deliveries into its bucket.

The Telegram callback lost its exemption for the same reason (#247) and is
IP-keyed under its own prefix.
"""

import time
from collections import defaultdict
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_EXEMPT_PREFIXES = ("/health", "/ws/")
# Checked before the /api/ gate so a future bare-"/webhook" mount stays covered.
_WEBHOOK_PREFIXES = ("/webhook", "/api/v1/webhook")
# The Telegram callback is unauthenticated-until-secret-token, exactly like
# the webhooks, so it is IP-keyed too (#247) — but under its own bucket
# prefix, so a noisy GitHub delivery stream cannot starve the bot and vice
# versa. It shares the generous webhook ceiling on purpose: Telegram retries
# every non-2xx delivery, our 429 included, so a throttled bot would keep
# re-arming its own limiter. Telegram delivers every chat's updates from
# its own egress IPs, so in practice all chats share one or two buckets and
# the 120/min ceiling is effectively a global one for the bot. That is
# still orders of magnitude above one operator chat's real traffic, which
# keeps legitimate updates clear of the ceiling; rejected updates get a
# cheap 204 from the handler rather than an error, so they never start a
# retry storm in the first place.
_TELEGRAM_PREFIXES = ("/api/v1/telegram/callback",)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, requests_per_minute: int = 60, webhook_requests_per_minute: int = 120):
        super().__init__(app)
        self._max_rpm = requests_per_minute
        self._webhook_max_rpm = webhook_requests_per_minute
        self._window = 60.0
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._request_count = 0

    async def _bucket_key(self, request: Request) -> str:
        """Resolve the caller to a bucket key; never raises.

        Runs pre-auth (middleware onion), so it resolves credentials itself:
        - Bearer via ``resolve_bearer_principal`` — shares the auth deps'
          30s caches (positive AND negative), so cold-cache DB hits are
          bounded to one per token per TTL. Its ``touch_last_used`` fires
          here and again in the auth dep; the repo throttles writes.
        - Session via ``request_session`` — pure itsdangerous decode, no DB.

        Deliberately does NOT set request.state.principal/session: the auth
        dependencies own those, and dev-mode bypass depends on them being
        absent (request_actor semantics).
        """
        from embry0.api.deps import resolve_bearer_principal
        from embry0.api.session import request_session

        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            try:
                principal = await resolve_bearer_principal(request.app, auth[7:])
            except Exception:
                principal = None  # limiter must never 500 — fall through to IP
            if principal is not None:
                if principal.kind == "master":
                    return "master"
                return f"key:{principal.key_id}"
        try:
            session = request_session(request)
        except Exception:
            session = None
        if session is not None and session.email:
            return f"user:{session.email.lower()}"
        return f"ip:{request.client.host if request.client else 'unknown'}"

    def _over_limit(self, key: str, max_rpm: int) -> bool:
        """Record a hit in *key*'s sliding window; True when the window is full."""
        now = time.time()
        self._requests[key] = [t for t in self._requests[key] if t > now - self._window]
        if len(self._requests[key]) >= max_rpm:
            return True
        self._requests[key].append(now)
        self._request_count += 1
        if self._request_count >= 1000:
            self._request_count = 0
            stale = [k for k, v in self._requests.items() if not v]
            for k in stale:
                del self._requests[k]
        return False

    @staticmethod
    def _too_many() -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded"},
            headers={"Retry-After": "60"},
        )

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        unauthenticated_prefix = None
        if any(path.startswith(p) for p in _WEBHOOK_PREFIXES):
            unauthenticated_prefix = "wh"
        elif any(path.startswith(p) for p in _TELEGRAM_PREFIXES):
            unauthenticated_prefix = "tg"
        if unauthenticated_prefix is not None:
            # IP-keyed (RAV-1070): webhooks carry no credential to bucket on
            # until their HMAC verifies inside the handler. The "wh"/"tg"
            # prefixes keep these buckets disjoint from each other and from
            # the authenticated ip: ones.
            ip = request.client.host if request.client else "unknown"
            if self._over_limit(f"{unauthenticated_prefix}:{ip}", self._webhook_max_rpm):
                return self._too_many()
            resp: Response = await call_next(request)
            return resp
        if not path.startswith("/api/"):
            resp = await call_next(request)
            return resp
        if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            resp = await call_next(request)
            return resp
        if self._over_limit(await self._bucket_key(request), self._max_rpm):
            return self._too_many()
        resp = await call_next(request)
        return resp
