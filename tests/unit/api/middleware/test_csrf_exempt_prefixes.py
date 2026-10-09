"""CSRF exempt-prefix guard (RAV-1070).

``CSRFMiddleware`` skips whole path prefixes, so any NEW mutating route added
under an exempt prefix silently inherits the bypass. This test enumerates the
real app's route table and fails when an unsafe-method route under an exempt
prefix is not in the reviewed allowlist below — forcing every such route to
document its independent auth mechanism before it ships.
"""

import pytest

from embry0.api.middleware.csrf import _EXEMPT_PREFIXES
from tests.fixtures.routes import app_routes

try:
    from embry0.api.app import create_app
except ImportError:
    pytest.skip("psycopg not available", allow_module_level=True)

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Every unsafe-method HTTP route (and every WebSocket route) living under a
# CSRF-exempt prefix, mapped to the independent auth that justifies the
# exemption. Adding a route under an exempt prefix REQUIRES a row here —
# review its auth story first; "it's internal" is not an auth story.
_REVIEWED_EXEMPT_ROUTES: dict[str, str] = {
    "/api/v1/webhook": "GitHub HMAC signature, sha256= prefixed (webhook_ingress.verify_webhook)",
    "/api/v1/webhook/linear": "Linear HMAC signature, bare hex (webhook_ingress.verify_webhook)",
    "/api/v1/webhook/gitlab": (
        "X-Gitlab-Token shared secret, compared with compare_digest "
        "(webhook_ingress.verify_webhook). Not an HMAC over the body — GitLab "
        "sends the secret itself, so the token is the whole auth story and a "
        "missing/empty GITLAB_WEBHOOK_SECRET refuses the request with 503 "
        "outside dev mode."
    ),
    "/api/v1/webhook/alertmanager": (
        "Authorization: Bearer shared secret, compared with compare_digest "
        "(webhook_ingress.verify_webhook, SHARED_SECRET scheme). Alertmanager "
        "cannot HMAC a body — its http_config offers bearer, basic auth or "
        "oauth2 and nothing else — so the token is the whole auth story, "
        "exactly as for GitLab. An empty ALERTMANAGER_WEBHOOK_SECRET refuses "
        "the request with 503 outside dev mode. LAN-only by deployment: "
        "Alertmanager runs beside the orchestrator and posts to it directly, "
        "so this path is never published through the tunnel."
    ),
    "/api/v1/telegram/callback": "X-Telegram-Bot-Api-Secret-Token (in-handler compare_digest)",
    "/api/v1/internal/qa/presign": "per-sandbox bearer token in body (SandboxTokenRegistry)",
    "/ws/jobs/{job_id}/events": "scoped-token subprotocol / SSO session (in-handler)",
}


def _exempt(path: str) -> bool:
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


def _collect_exempt_routes() -> dict[str, str]:
    """Return {path: kind} for routes the CSRF middleware never sees."""
    found: dict[str, str] = {}
    for route in app_routes(create_app()):
        if not _exempt(route.path):
            continue
        if route.methods is None:
            # WebSocket route — no CSRF protection possible; the handler
            # must authenticate the handshake itself.
            found[route.path] = "websocket"
        elif route.methods & _UNSAFE_METHODS:
            found[route.path] = "http"
    return found


def test_every_csrf_exempt_mutating_route_is_reviewed():
    unreviewed = set(_collect_exempt_routes()) - set(_REVIEWED_EXEMPT_ROUTES)
    assert not unreviewed, (
        f"Mutating routes under CSRF-exempt prefixes lack a reviewed auth story: {sorted(unreviewed)}. "
        "Each needs independent authentication (HMAC, scoped token, sandbox bearer, ...) — "
        "document it in _REVIEWED_EXEMPT_ROUTES in this test after review."
    )


def test_reviewed_allowlist_has_no_stale_entries():
    stale = set(_REVIEWED_EXEMPT_ROUTES) - set(_collect_exempt_routes())
    assert not stale, f"_REVIEWED_EXEMPT_ROUTES entries no longer in the app: {sorted(stale)}"
