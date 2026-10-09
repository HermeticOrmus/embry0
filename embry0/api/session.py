"""Signed-cookie session for Auth0 SSO (RAV-1069).

Stateless: the cookie carries the identity + folded tier, signed with
``SESSION_SECRET`` (itsdangerous timed serializer). No server-side session
store — role changes in Auth0 apply at the next login, and rotating
``SESSION_SECRET`` invalidates every outstanding session.

Tier folding is DEFAULT-DENY, deliberately diverging from the
just-in-time-provisioning pattern: embry0 holds repo credentials and can push
code, so an identity that authenticates but carries no embry0 role gets
``none`` and is refused by the auth dependency (403), not granted a default
tier.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = "embry0_session"
STATE_COOKIE = "embry0_oidc_state"
STATE_MAX_AGE_SECONDS = 600

ROLE_ADMIN = "embry0-admin"
ROLE_OPERATOR = "embry0-operator"

TIER_ADMIN = "admin"
TIER_OPERATOR = "operator"
TIER_NONE = "none"


@dataclass(frozen=True, slots=True)
class SessionData:
    sub: str
    email: str
    name: str
    roles: list[str]
    tier: str
    # RAV-1056 Phase A: the users-row session_epoch this cookie was minted
    # under. A bumped DB epoch invalidates the cookie (force-logout). Cookies
    # minted before Phase A decode as epoch 0 and fail the gate once —
    # a single forced re-login on upgrade.
    epoch: int = 0


def fold_tier(roles: list[str]) -> str:
    """Fold the Auth0 roles claim into embry0's access tier (default-deny)."""
    normalized = {str(r).strip().lower() for r in roles}
    if ROLE_ADMIN in normalized:
        return TIER_ADMIN
    if ROLE_OPERATOR in normalized:
        return TIER_OPERATOR
    return TIER_NONE


def _serializer(secret: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=salt)


def encode_session(secret: str, data: SessionData) -> str:
    return _serializer(secret, "embry0-session-v1").dumps(asdict(data))


def decode_session(secret: str, value: str, max_age: int) -> SessionData | None:
    """Return the session or None for a missing/expired/tampered cookie."""
    if not value or not secret:
        return None
    try:
        raw: dict[str, Any] = _serializer(secret, "embry0-session-v1").loads(value, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    try:
        return SessionData(
            sub=str(raw["sub"]),
            email=str(raw["email"]),
            name=str(raw.get("name", "")),
            roles=[str(r) for r in raw.get("roles", [])],
            tier=str(raw.get("tier", TIER_NONE)),
            epoch=int(raw.get("epoch", 0)),
        )
    except (KeyError, TypeError):
        return None


def request_session(request: Any) -> SessionData | None:
    """Decode the session cookie on any request (shared by deps, SSE, WS).

    Defensive getattr: unit tests (and any embedder) hand minimal duck-typed
    config stubs — a config without the SSO fields simply means SSO is off.
    """
    config = request.app.state.config
    if not getattr(config, "sso_enabled", False):
        return None
    return decode_session(
        str(config.session_secret),
        str(request.cookies.get(SESSION_COOKIE, "") or ""),
        max_age=int(config.sso_session_max_age_seconds),
    )


def encode_state(secret: str, payload: dict[str, str]) -> str:
    """Sign the transient OIDC round-trip state (state/nonce/PKCE verifier)."""
    return _serializer(secret, "embry0-oidc-state-v1").dumps(payload)


def decode_state(secret: str, value: str) -> dict[str, str] | None:
    if not value or not secret:
        return None
    try:
        raw = _serializer(secret, "embry0-oidc-state-v1").loads(value, max_age=STATE_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else None
