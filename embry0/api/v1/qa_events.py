"""SSE liveness stream for /api/v1/qa/runs/{run_id}/events.

Phase 5C of the QA dashboard pending-frontend plan: the dashboard polls
``GET /api/v1/qa/runs/{run_id}`` every 15 s today; this route lets the
frontend open an EventSource so per-app status changes propagate within a
heartbeat instead of after the next poll. Polling stays as the fallback —
when an EventSource disconnects (auth, network, server restart) the 15 s
poller in ``useQaRun`` keeps the dashboard live.

Auth: browsers cannot send custom headers on EventSource, so the stream
accepts a ``token`` query parameter — but never the master API key
(RAV-1060: a query-borne master key lands in nginx access logs, browser
history, and Referer; one log line was a full credential compromise). The
client first POSTs ``/qa/runs/{run_id}/events-token`` (normal Bearer auth)
for a short-lived HMAC token scoped to that single run, and opens the
EventSource with it. The Bearer-header path on the stream itself remains
for non-browser clients. If auth fails (expired token, stale key), the
dashboard's 15 s polling fallback covers liveness.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from embry0.api.deps import require_auth
from embry0.api.scoped_tokens import mint_scoped_token, signing_material, verify_scoped_token
from embry0.api.session import TIER_ADMIN, TIER_OPERATOR, request_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter()

__all__ = ["mint_sse_token", "router", "signing_material", "verify_sse_token"]


def mint_sse_token(api_key: str, run_id: str, *, now: float | None = None) -> tuple[str, int]:
    """Mint a run-scoped SSE token. Returns ``(token, expires_in_seconds)``.

    Thin wrapper over the shared scoped-token module (RAV-1070) with the SSE
    scope namespace; ``api_key`` is the signing material (see
    :func:`embry0.api.scoped_tokens.signing_material`).
    """
    return mint_scoped_token(api_key, f"sse:{run_id}", now=now)


def verify_sse_token(api_key: str, run_id: str, token: str, *, now: float | None = None) -> bool:
    """Constant-time verify of a run-scoped token: version, expiry, signature."""
    return verify_scoped_token(api_key, f"sse:{run_id}", token, now=now)


async def _auth_header_or_scoped_token(
    request: Request,
    run_id: str,
    token: str | None = Query(default=None, description="Run-scoped SSE token from POST .../events-token"),
) -> None:
    """Require a Bearer header OR a valid run-scoped ``token`` query param.

    The master API key is deliberately NOT accepted via the query string
    (RAV-1060) — only the short-lived scoped token rides the URL.
    """
    config = request.app.state.config
    # #518: only AUTH_DEV_MODE skips this, never an empty API_KEY.
    if config.auth_dev_mode:
        return
    if token is not None and verify_sse_token(signing_material(config), run_id, token):
        return
    # RAV-1069: an SSO session (operator+) authenticates the stream directly —
    # same-origin EventSource sends the cookie with no header plumbing.
    # RAV-1056: subject to the users-table gate (disabled / stale epoch).
    session = request_session(request)
    if session is not None and session.tier in (TIER_ADMIN, TIER_OPERATOR):
        from embry0.api.deps import validate_session_user

        if await validate_session_user(request.app, session):
            return
    # RAV-1056: any live key (>= read) authenticates the stream — the
    # master-only literal compare is gone.
    from embry0.api.deps import resolve_bearer_principal

    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        principal = await resolve_bearer_principal(request.app, authorization[7:])
        if principal is not None:
            return
    raise HTTPException(status_code=401, detail="Missing or invalid credentials")


@router.post("/qa/runs/{run_id}/events-token", dependencies=[require_auth])
async def create_events_token(run_id: str, request: Request) -> dict[str, Any]:
    """Mint a short-lived, single-run SSE token for EventSource clients.

    Authenticated with the normal Bearer header; the returned token is the
    only credential the browser puts in the SSE URL.
    """
    token, expires_in = mint_sse_token(signing_material(request.app.state.config), run_id)
    return {"token": token, "expires_in": expires_in}


@router.get("/qa/runs/{run_id}/events", dependencies=[Depends(_auth_header_or_scoped_token)])
async def stream_run_events(run_id: str, request: Request) -> EventSourceResponse:
    """Server-Sent Events stream of QA state changes for a single run.

    Closes when the run finishes (final ``done`` event) or the client
    disconnects. Events are JSON-encoded; clients parse ``event.data``.

    Returns 503 if the QA event bus is not initialised — happens in tests
    that build a FastAPI app without entering the lifespan, and in any
    misconfigured deploy that skipped ``app.state.qa_event_bus = ...``.
    """
    bus = getattr(request.app.state, "qa_event_bus", None)
    if bus is None:
        raise HTTPException(status_code=503, detail="qa event bus not initialised")

    async def event_stream() -> AsyncIterator[dict[str, Any]]:
        # ``bus.subscribe`` returns an async generator that registers a queue
        # on entry and prunes it in a finally-block on exit. Iterating with
        # ``async for`` ensures the cleanup runs even when the client
        # disconnects (sse-starlette cancels the task, which raises
        # CancelledError into the await — propagated through the generator
        # finally so the channel doesn't leak).
        async for event in bus.subscribe(run_id):
            # Belt-and-suspenders disconnect check; sse-starlette also
            # cancels the task on disconnect, but checking explicitly lets
            # us close the iterator cleanly between events.
            if await request.is_disconnected():
                return
            yield {"data": json.dumps(event)}
            if event.get("type") == "done":
                return

    return EventSourceResponse(event_stream())
