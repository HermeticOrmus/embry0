"""Unit tests for the xAI OAuth credential store + refresher (EMB-45)."""

from __future__ import annotations

import asyncio
import json

import pytest

from embry0.execution.xai_credential import (
    DurableState,
    XaiCredentialError,
    XaiCredentialStore,
    XaiTokenRefresher,
    build_refresher,
)
from embry0.storage.encryption import FernetSecretsProvider

_SECRET = "unit-test-secret-key-not-real"


@pytest.fixture(scope="module")
def secrets() -> FernetSecretsProvider:
    # PBKDF2 is slow; build the provider once for the module.
    return FernetSecretsProvider(secret_key=_SECRET)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None, *, json_ok: bool = True) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_ok = json_ok

    def json(self) -> dict:
        if not self._json_ok:
            raise ValueError("not json")
        return self._payload or {}


class _FakeHttp:
    """Records POSTs and returns queued responses (or a default)."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def post(self, url: str, *, data: dict) -> _FakeResponse:
        self.calls.append({"url": url, "data": dict(data)})
        if self._responses:
            return self._responses.pop(0)
        raise AssertionError("unexpected extra POST")


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _grok_cli_store(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(
        json.dumps(
            {
                "https://auth.x.ai::client-abc": {
                    "refresh_token": "rt-seed-0",
                    "oidc_client_id": "client-abc",
                    "oidc_issuer": "https://auth.x.ai",
                    "email": "user@example.com",
                }
            }
        ),
        encoding="utf-8",
    )
    return p


# ---- Store ---------------------------------------------------------------


async def test_store_roundtrip(tmp_path, secrets):
    store = XaiCredentialStore(tmp_path / "cred.enc", secrets)
    assert await store.load() is None
    state = DurableState("rt-1", "client-abc", "https://auth.x.ai/oauth2/token")
    await store.save(state)
    assert await store.load() == state
    # File is encrypted at rest — the plaintext refresh token must not appear.
    assert "rt-1" not in (tmp_path / "cred.enc").read_text(encoding="utf-8")


async def test_store_corrupt_raises(tmp_path, secrets):
    path = tmp_path / "cred.enc"
    path.write_text("not-a-fernet-token", encoding="utf-8")
    store = XaiCredentialStore(path, secrets)
    with pytest.raises(XaiCredentialError):
        await store.load()


async def test_seed_from_grok_cli(tmp_path, secrets):
    store = XaiCredentialStore(tmp_path / "cred.enc", secrets)
    state = await store.seed_from_grok_cli(_grok_cli_store(tmp_path))
    assert state.refresh_token == "rt-seed-0"
    assert state.client_id == "client-abc"
    assert state.token_endpoint == "https://auth.x.ai/oauth2/token"
    assert await store.load() == state


async def test_seed_missing_fields_raises(tmp_path, secrets):
    bad = tmp_path / "auth.json"
    bad.write_text(json.dumps({"k": {"oidc_client_id": "c", "oidc_issuer": "https://auth.x.ai"}}))
    store = XaiCredentialStore(tmp_path / "cred.enc", secrets)
    with pytest.raises(XaiCredentialError):
        await store.seed_from_grok_cli(bad)


# ---- Refresher -----------------------------------------------------------


async def _seeded_store(tmp_path, secrets) -> XaiCredentialStore:
    store = XaiCredentialStore(tmp_path / "cred.enc", secrets)
    await store.save(DurableState("rt-0", "client-abc", "https://auth.x.ai/oauth2/token"))
    return store


async def test_refresh_returns_access_token_and_rotates(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    http = _FakeHttp([_FakeResponse(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 21600})])
    clock = _Clock()
    ref = XaiTokenRefresher(store, http_client=http, clock=clock)

    token = await ref.ensure_fresh()
    assert token == "at-1"
    assert len(http.calls) == 1
    assert http.calls[0]["data"]["grant_type"] == "refresh_token"
    assert http.calls[0]["data"]["refresh_token"] == "rt-0"
    # Rotated refresh token is persisted.
    assert (await store.load()).refresh_token == "rt-1"


async def test_cached_token_reused_within_margin(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    http = _FakeHttp([_FakeResponse(200, {"access_token": "at-1", "expires_in": 21600})])
    clock = _Clock()
    ref = XaiTokenRefresher(store, http_client=http, clock=clock)

    assert await ref.ensure_fresh() == "at-1"
    clock.advance(60)  # well within the 6h lifetime minus 30m margin
    assert await ref.ensure_fresh() == "at-1"
    assert len(http.calls) == 1  # no second refresh


async def test_forced_refresh_ignores_the_expiry_margin(tmp_path, secrets):
    """xAI can call a token stale long before its expiry. The orchestrator then forces a
    refresh; without force, ensure_fresh keeps serving the token xAI just refused."""
    store = await _seeded_store(tmp_path, secrets)
    http = _FakeHttp(
        [
            _FakeResponse(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 21600}),
            _FakeResponse(200, {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 21600}),
        ]
    )
    clock = _Clock()
    ref = XaiTokenRefresher(store, http_client=http, clock=clock)

    assert await ref.ensure_fresh() == "at-1"
    clock.advance(60)
    assert await ref.ensure_fresh(force=True) == "at-2", "force must refresh a token that is not near expiry"
    assert len(http.calls) == 2
    assert http.calls[1]["data"]["refresh_token"] == "rt-1", "the forced refresh presents the rotated token"


async def test_refresh_again_near_expiry_uses_rotated_token(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    http = _FakeHttp(
        [
            _FakeResponse(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}),
            _FakeResponse(200, {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}),
        ]
    )
    clock = _Clock()
    ref = XaiTokenRefresher(store, http_client=http, clock=clock)

    assert await ref.ensure_fresh() == "at-1"
    clock.advance(3600 - 60)  # inside the 30m refresh margin
    assert await ref.ensure_fresh() == "at-2"
    assert len(http.calls) == 2
    # Second refresh must present the rotated token from the first.
    assert http.calls[1]["data"]["refresh_token"] == "rt-1"
    assert (await store.load()).refresh_token == "rt-2"


async def test_concurrent_ensure_fresh_serializes(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    http = _FakeHttp([_FakeResponse(200, {"access_token": "at-1", "expires_in": 21600})])
    clock = _Clock()
    ref = XaiTokenRefresher(store, http_client=http, clock=clock)

    results = await asyncio.gather(*[ref.ensure_fresh() for _ in range(5)])
    assert results == ["at-1"] * 5
    assert len(http.calls) == 1  # only one refresh despite 5 concurrent callers


async def test_refresh_non_200_raises(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    ref = XaiTokenRefresher(store, http_client=_FakeHttp([_FakeResponse(403, {"error": "denied"})]))
    with pytest.raises(XaiCredentialError):
        await ref.ensure_fresh()


async def test_refresh_missing_access_token_raises(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    ref = XaiTokenRefresher(store, http_client=_FakeHttp([_FakeResponse(200, {"refresh_token": "x"})]))
    with pytest.raises(XaiCredentialError):
        await ref.ensure_fresh()


async def test_refresh_without_seed_raises(tmp_path, secrets):
    store = XaiCredentialStore(tmp_path / "cred.enc", secrets)  # never seeded
    ref = XaiTokenRefresher(store, http_client=_FakeHttp([]))
    with pytest.raises(XaiCredentialError):
        await ref.ensure_fresh()


# ---- build_refresher -----------------------------------------------------


async def test_build_refresher_seeds_on_first_run(tmp_path, secrets):
    cli = _grok_cli_store(tmp_path)
    http = _FakeHttp([_FakeResponse(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 21600})])
    ref = await build_refresher(
        store_path=tmp_path / "cred.enc",
        secret_key=_SECRET,
        grok_cli_store=cli,
        http_client=http,
    )
    assert await ref.ensure_fresh() == "at-1"
    # First refresh presented the seeded token.
    assert http.calls[0]["data"]["refresh_token"] == "rt-seed-0"


async def test_build_refresher_requires_secret_key(tmp_path):
    with pytest.raises(XaiCredentialError):
        await build_refresher(store_path=tmp_path / "cred.enc", secret_key="")


# ---- Token-endpoint pinning (RAV-1284) -----------------------------------


@pytest.mark.parametrize(
    "issuer",
    [
        "http://auth.x.ai",  # not HTTPS
        "https://evil.example.com",  # wrong host
        "https://notx.ai",  # suffix trap: must not match *.x.ai
    ],
)
async def test_seed_rejects_unpinned_issuer(tmp_path, secrets, issuer):
    bad = tmp_path / "auth.json"
    bad.write_text(
        json.dumps({f"{issuer}::c": {"refresh_token": "rt", "oidc_client_id": "c", "oidc_issuer": issuer}}),
        encoding="utf-8",
    )
    store = XaiCredentialStore(tmp_path / "cred.enc", secrets)
    with pytest.raises(XaiCredentialError, match="refusing to send the refresh token"):
        await store.seed_from_grok_cli(bad)
    assert await store.load() is None  # nothing persisted


async def test_refresh_rejects_unpinned_persisted_endpoint(tmp_path, secrets):
    store = XaiCredentialStore(tmp_path / "cred.enc", secrets)
    await store.save(DurableState("rt-0", "client-abc", "https://evil.example.com/oauth2/token"))
    http = _FakeHttp([])
    ref = XaiTokenRefresher(store, http_client=http)
    with pytest.raises(XaiCredentialError, match="refusing to send the refresh token"):
        await ref.ensure_fresh()
    assert http.calls == []  # the refresh token never left the process


# ---- Adaptive refresh margin (RAV-1284) ----------------------------------


async def test_short_ttl_token_does_not_refresh_every_call(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    http = _FakeHttp(
        [
            _FakeResponse(200, {"access_token": "at-1", "expires_in": 900}),
            _FakeResponse(200, {"access_token": "at-2", "expires_in": 900}),
        ]
    )
    clock = _Clock()
    ref = XaiTokenRefresher(store, http_client=http, clock=clock)

    assert await ref.ensure_fresh() == "at-1"
    clock.advance(60)  # inside the fixed 30m margin, but the margin scaled to ttl//3 = 300s
    assert await ref.ensure_fresh() == "at-1"
    assert len(http.calls) == 1  # no per-call refresh-token burn
    clock.advance(590)  # now within 300s of expiry
    assert await ref.ensure_fresh() == "at-2"
    assert len(http.calls) == 2


# ---- Invalid-grant self-healing + 403 decoration (RAV-1284) --------------


async def test_terminal_refresh_reseeds_from_cli_and_retries(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    cli = _grok_cli_store(tmp_path)
    http = _FakeHttp(
        [
            _FakeResponse(400, {"error": "invalid_grant"}),
            _FakeResponse(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 21600}),
        ]
    )
    ref = XaiTokenRefresher(store, http_client=http, grok_cli_store=cli)

    assert await ref.ensure_fresh() == "at-1"
    assert len(http.calls) == 2
    assert http.calls[0]["data"]["refresh_token"] == "rt-0"
    assert http.calls[1]["data"]["refresh_token"] == "rt-seed-0"  # the re-seeded lineage
    assert (await store.load()).refresh_token == "rt-1"  # rotated on the retried lineage


async def test_terminal_refresh_with_dead_cli_store_raises_recovery_message(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    cli = _grok_cli_store(tmp_path)
    http = _FakeHttp(
        [
            _FakeResponse(400, {"error": "invalid_grant"}),
            _FakeResponse(400, {"error": "invalid_grant"}),  # re-seeded token is dead too
        ]
    )
    ref = XaiTokenRefresher(store, http_client=http, grok_cli_store=cli)
    with pytest.raises(XaiCredentialError, match="grok login"):
        await ref.ensure_fresh()
    assert len(http.calls) == 2  # exactly one retry, no loop


async def test_terminal_refresh_without_cli_store_raises_immediately(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    http = _FakeHttp([_FakeResponse(400, {"error": "invalid_grant"})])
    ref = XaiTokenRefresher(store, http_client=http)  # no grok_cli_store configured
    with pytest.raises(XaiCredentialError, match="grok login"):
        await ref.ensure_fresh()
    assert len(http.calls) == 1


async def test_terminal_refresh_unreadable_cli_store_raises_combined_error(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    http = _FakeHttp([_FakeResponse(400, {"error": "invalid_grant"})])
    ref = XaiTokenRefresher(store, http_client=http, grok_cli_store=tmp_path / "missing.json")
    with pytest.raises(XaiCredentialError, match="re-seed .* also failed"):
        await ref.ensure_fresh()
    assert len(http.calls) == 1


async def test_403_error_is_decorated_with_entitlement_hint(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    ref = XaiTokenRefresher(store, http_client=_FakeHttp([_FakeResponse(403, {"error": "denied"})]))
    with pytest.raises(XaiCredentialError, match="SuperGrok"):
        await ref.ensure_fresh()


async def test_5xx_does_not_consume_reseed(tmp_path, secrets):
    store = await _seeded_store(tmp_path, secrets)
    cli = _grok_cli_store(tmp_path)
    http = _FakeHttp([_FakeResponse(503, {"error": "unavailable"})])
    ref = XaiTokenRefresher(store, http_client=http, grok_cli_store=cli)
    with pytest.raises(XaiCredentialError):
        await ref.ensure_fresh()
    assert len(http.calls) == 1  # transient failure: no re-seed, no retry
    assert (await store.load()).refresh_token == "rt-0"  # lineage untouched
