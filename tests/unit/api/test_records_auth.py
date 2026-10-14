"""Who may read, write and delete repo records.

Reads: every tier. Writes: operator and above. Deletes: admin. A key limited
to a repository allowlist reaches only the records of its repositories, reads
included. Driven through the real app with a fake records repository, so the
route wiring and the tier checks are what is measured.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from embry0.api.deps import bust_api_key_cache
from embry0.config import Embry0Config
from embry0.storage.repositories.api_keys import ApiKeyRow, hash_token

ALLOWED = "acme/widgets"
OTHER = "group/sub/secret"

READ = "emb_" + "r" * 43
OPERATE = "emb_" + "o" * 43
ADMIN = "emb_" + "a" * 43
LIMITED = "emb_" + "l" * 43


def _key(key_id: str, scope: str, allowed: tuple[str, ...] | None = None) -> ApiKeyRow:
    return ApiKeyRow(
        id=key_id,
        user_id=None,
        name=key_id,
        prefix="emb_xxxxxxxx",
        scope=scope,
        created_by="tester",
        created_at=None,
        expires_at=None,
        last_used_at=None,
        revoked_at=None,
        allowed_repos=allowed,
    )


KEYS = {
    hash_token(READ): _key("key-read", "read"),
    hash_token(OPERATE): _key("key-operate", "operate"),
    hash_token(ADMIN): _key("key-admin", "admin"),
    hash_token(LIMITED): _key("key-limited", "admin", (ALLOWED,)),
}

_RECORD = {"repo": ALLOWED, "slug": "a", "kind": "note", "title": "a", "revision": 1, "body": "x"}


@pytest.fixture
def client() -> Any:
    from embry0.api.app import create_app

    bust_api_key_cache()
    with patch.dict(os.environ, {}, clear=True):
        config = Embry0Config(_env_file=None, auth_dev_mode=False, webhook_dev_mode=False, api_key="master-key")
    app = create_app(config)
    state = app.state
    state.db = None
    state.users_repo = None
    state.provider_repo = None
    keys = MagicMock()
    keys.get_by_hash = AsyncMock(side_effect=lambda h: KEYS.get(h))
    keys.touch_last_used = AsyncMock()
    state.api_keys_repo = keys

    records = MagicMock()
    records.list_records = AsyncMock(return_value=[])
    records.get = AsyncMock(return_value=_RECORD)
    records.revisions = AsyncMock(return_value=[])
    records.put = AsyncMock(return_value={**_RECORD, "created": False, "changed": False})
    records.delete = AsyncMock(return_value=True)
    state.repo_records_repo = records
    # No context manager: the lifespan would connect to a real stack.
    c = TestClient(app)
    c.records = records
    yield c
    bust_api_key_cache()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _calls(repo: str) -> list[tuple[str, str, Any]]:
    base = f"/api/v1/repos/{repo}/records"
    return [
        ("GET", base, None),
        ("GET", f"{base}/a", None),
        ("GET", f"{base}/a/revisions", None),
        ("PUT", f"{base}/a", {"kind": "note", "body": "x"}),
        ("DELETE", f"{base}/a", None),
    ]


@pytest.mark.parametrize(
    ("token", "allowed_methods"),
    [
        (READ, {"GET"}),
        (OPERATE, {"GET", "PUT"}),
        (ADMIN, {"GET", "PUT", "DELETE"}),
    ],
)
def test_tiers(client: Any, token: str, allowed_methods: set[str]) -> None:
    for method, path, body in _calls(ALLOWED):
        r = client.request(method, path, json=body, headers=_auth(token))
        if method in allowed_methods:
            assert r.status_code < 300, f"{method} {path} should be allowed: {r.status_code} {r.text}"
        else:
            assert r.status_code == 403, f"{method} {path} should be refused for this tier: {r.status_code}"


def test_unauthenticated_is_refused(client: Any) -> None:
    for method, path, body in _calls(ALLOWED):
        r = client.request(method, path, json=body, headers={"X-Requested-With": "XMLHttpRequest"})
        assert r.status_code == 401, f"{method} {path}: {r.status_code}"


def test_allowlisted_key_reaches_only_its_repos(client: Any) -> None:
    for method, path, body in _calls(ALLOWED):
        r = client.request(method, path, json=body, headers=_auth(LIMITED))
        assert r.status_code < 300, f"{method} {path} on an allowed repo: {r.status_code} {r.text}"
    client.records.reset_mock()
    for method, path, body in _calls(OTHER):
        r = client.request(method, path, json=body, headers=_auth(LIMITED))
        assert r.status_code == 403, f"{method} {path} on another repo must be refused: {r.status_code}"
        assert r.json()["detail"].startswith("repo_not_allowed")
    for name in ("list_records", "get", "revisions", "put", "delete"):
        assert not getattr(client.records, name).await_count, f"records.{name} ran for a refused repo"


def test_put_needs_the_csrf_header(client: Any) -> None:
    r = client.put(
        f"/api/v1/repos/{ALLOWED}/records/a",
        json={"kind": "note", "body": "x"},
        headers={"Authorization": f"Bearer {OPERATE}"},
    )
    assert r.status_code == 403, f"a mutation without X-Requested-With must be refused: {r.status_code}"


def test_writer_is_the_caller(client: Any) -> None:
    client.put(f"/api/v1/repos/{ALLOWED}/records/a", json={"kind": "note", "body": "x"}, headers=_auth(OPERATE))
    assert client.records.put.await_args.kwargs["author"] == "key:key-operate"
