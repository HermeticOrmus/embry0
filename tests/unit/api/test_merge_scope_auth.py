"""The `merge` key scope (#319 phase 2): read, decide, answer — nothing else.

The chat relay holds this key for the approval chat. The point of the scope is
that a leaked copy cannot cancel jobs, create them, edit env vars or trigger
the watcher, while the chat keeps working: merge decisions and agent answers.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from embry0.api.deps import MERGE_SCOPE_MUTATIONS, bust_api_key_cache, require_admin, require_auth
from embry0.config import Embry0Config
from embry0.storage.repositories.api_keys import ApiKeyRow, hash_token
from tests.fixtures.routes import app_routes

TOKEN = "emb_" + "m" * 43


def _key(scope: str) -> ApiKeyRow:
    return ApiKeyRow(
        id="key-merge0000001",
        user_id=None,
        name="hermes",
        prefix="emb_mmmmmmmm",
        scope=scope,
        created_by="tester",
        created_at=None,
        expires_at=None,
        last_used_at=None,
        revoked_at=None,
    )


def _client(scope: str) -> TestClient:
    app = FastAPI()
    with patch.dict(os.environ, {}, clear=True):
        app.state.config = Embry0Config(_env_file=None, auth_dev_mode=False, api_key="master-key")
    keys = MagicMock()
    keys.get_by_hash = AsyncMock(side_effect=lambda h: _key(scope) if h == hash_token(TOKEN) else None)
    keys.touch_last_used = AsyncMock()
    app.state.api_keys_repo = keys
    app.state.users_repo = MagicMock()

    router = APIRouter()

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str):
        return {"ok": True}

    @router.post("/merge-approvals/{approval_id}/decide")
    def decide(approval_id: str):
        return {"ok": True}

    @router.post("/jobs/{job_id}/inputs/{input_id}/answer")
    def answer(job_id: str, input_id: str):
        return {"ok": True}

    @router.post("/cases/{case_id}/accept")
    def accept(case_id: str):
        return {"ok": True}

    @router.post("/jobs")
    def create_job():
        return {"ok": True}

    @router.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return {"ok": True}

    @router.put("/repos/{owner}/{repo}/env")
    def env(owner: str, repo: str):
        return {"ok": True}

    @router.post("/watcher/run")
    def watcher():
        return {"ok": True}

    @router.get("/agents/{agent_type}/secret", dependencies=[require_admin])
    def admin_only(agent_type: str):
        return {"ok": True}

    app.include_router(router, prefix="/api/v1", dependencies=[require_auth])
    return TestClient(app)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _clear_cache():
    bust_api_key_cache()
    yield
    bust_api_key_cache()


def test_a_merge_key_can_read() -> None:
    assert _client("merge").get("/api/v1/jobs/job-1", headers=AUTH).status_code == 200


def test_a_merge_key_can_decide_and_answer() -> None:
    client = _client("merge")
    assert client.post("/api/v1/merge-approvals/mrg-1/decide", headers=AUTH).status_code == 200
    assert client.post("/api/v1/jobs/job-1/inputs/inp-1/answer", headers=AUTH).status_code == 200


def test_a_merge_key_can_accept_a_watcher_finding() -> None:
    """#378: the chat bot accepts this deployment's own findings with the
    same narrow key; the route itself refuses everything but those."""
    assert _client("merge").post("/api/v1/cases/hc-1/accept", headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/jobs"),
        ("POST", "/api/v1/jobs/job-1/cancel"),
        ("PUT", "/api/v1/repos/o/r/env"),
        ("POST", "/api/v1/watcher/run"),
        # An id that spells an allowed route must not borrow its permission.
        ("POST", "/api/v1/jobs/merge-approvals/cancel"),
    ],
)
def test_a_merge_key_cannot_make_any_other_mutation(method: str, path: str) -> None:
    response = _client("merge").request(method, path, headers=AUTH)
    assert response.status_code == 403, f"{method} {path} was allowed for a merge-scope key"
    assert "merge-scope" in response.json()["detail"]


def test_a_merge_key_is_not_admin() -> None:
    assert _client("merge").get("/api/v1/agents/x/secret", headers=AUTH).status_code == 403


def test_an_operate_key_is_unaffected() -> None:
    """The restriction is the merge scope's, not a new rule for every key."""
    client = _client("operate")
    assert client.post("/api/v1/jobs", headers=AUTH).status_code == 200
    assert client.post("/api/v1/merge-approvals/mrg-1/decide", headers=AUTH).status_code == 200


def test_every_allowed_mutation_is_a_real_route() -> None:
    """A template that matches no route would make the allowlist silently
    narrower than it reads — e.g. the relay losing job_answer after a rename."""
    from embry0.api.app import create_app

    routes = {
        (method, route.path)
        for route in app_routes(create_app())
        if route.is_api_route
        for method in route.methods or ()
    }
    missing = sorted(m for m in MERGE_SCOPE_MUTATIONS if m not in routes)
    assert not missing, f"MERGE_SCOPE_MUTATIONS names routes the app does not have: {missing}"


@asynccontextmanager
async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield


def _real_app_client(scope: str) -> TestClient:
    """create_app() itself: its routers sit under an include prefix, the part
    of the template FastAPI 0.137 left out of scope["route"] (#502)."""
    from embry0.api.app import create_app

    with patch.dict(os.environ, {}, clear=True):
        config = Embry0Config(_env_file=None, auth_dev_mode=False, api_key="master-key")
    app = create_app(config, lifespan_override=_no_lifespan)
    keys = MagicMock()
    keys.get_by_hash = AsyncMock(side_effect=lambda h: _key(scope) if h == hash_token(TOKEN) else None)
    keys.touch_last_used = AsyncMock()
    app.state.api_keys_repo = keys
    app.state.users_repo = MagicMock()
    # No lifespan, so a handler that reaches for app.state may answer 500.
    # The scope check runs before it, and its 403 is all this reads.
    return TestClient(app, raise_server_exceptions=False, headers={"X-Requested-With": "XMLHttpRequest"})


def test_a_merge_key_reaches_every_allowed_mutation_of_the_real_app() -> None:
    client = _real_app_client("merge")
    blocked = client.post("/api/v1/watcher/run", headers=AUTH)
    assert blocked.status_code == 403 and "merge-scope" in blocked.text, (
        f"precondition: an ordinary mutation must be refused to a merge-scope key, got {blocked.status_code}"
    )
    refused = []
    for method, template in sorted(MERGE_SCOPE_MUTATIONS):
        response = client.request(method, re.sub(r"\{[^}]+\}", "x1", template), headers=AUTH)
        if response.status_code == 403 and "merge-scope" in response.text:
            refused.append(f"{method} {template}")
    assert not refused, f"a merge-scope key is refused routes on its own allowlist: {refused}"
