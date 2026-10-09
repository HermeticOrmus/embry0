"""Tests for startup-time configuration resolution."""

import os
from typing import Any
from unittest.mock import patch

import pytest
import structlog

from embry0.api.app import (
    _check_api_key,
    _resolve_proxy_admin_token,
    _resolve_secrets_provider,
    _warn_unknown_env_vars,
)
from embry0.config import Embry0Config


def test_proxy_admin_token_passed_through_when_set():
    """Token is passed through unchanged if already set."""
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(
            _env_file=None,
            auth_dev_mode=False,
            proxy_admin_token="preset-token-1234567890ab",
        )
    _resolve_proxy_admin_token(cfg)
    assert cfg.proxy_admin_token == "preset-token-1234567890ab"


def test_proxy_admin_token_auto_generated_in_auth_dev_mode():
    """Token is auto-generated when auth_dev_mode=True and token is empty."""
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(
            _env_file=None,
            auth_dev_mode=True,
            proxy_admin_token="",
        )
    _resolve_proxy_admin_token(cfg)
    # token_urlsafe(32) generates a ~43 character token
    assert len(cfg.proxy_admin_token) >= 40
    assert cfg.proxy_admin_token != ""


def test_proxy_admin_token_required_in_production():
    """RuntimeError is raised when auth_dev_mode=False and token is empty."""
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(
            _env_file=None,
            auth_dev_mode=False,
            proxy_admin_token="",
        )
    with pytest.raises(RuntimeError, match="PROXY_ADMIN_TOKEN must be set in production"):
        _resolve_proxy_admin_token(cfg)


def test_proxy_admin_token_auto_generated_in_webhook_dev_mode():
    """smee.io workflow: webhook_dev_mode alone should still trigger auto-gen."""
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(_env_file=None, auth_dev_mode=False, webhook_dev_mode=True, proxy_admin_token="")
    _resolve_proxy_admin_token(cfg)
    assert cfg.proxy_admin_token != ""


def test_proxy_admin_token_required_when_no_dev_mode():
    """Production posture: both flags off, missing token → refuse to start."""
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(_env_file=None, auth_dev_mode=False, webhook_dev_mode=False, proxy_admin_token="")
    with pytest.raises(RuntimeError, match="must be set in production"):
        _resolve_proxy_admin_token(cfg)


# ---------------------------------------------------------------------------
# _check_api_key tests
# ---------------------------------------------------------------------------


def test_api_key_passes_when_set():
    """A configured API_KEY passes the guard in production posture."""
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(_env_file=None, auth_dev_mode=False, webhook_dev_mode=False, api_key="a-real-key")
    _check_api_key(cfg)  # no raise


def test_api_key_empty_allowed_in_auth_dev_mode():
    """auth_dev_mode bypasses API auth anyway — empty key must not block boot."""
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=False, api_key="")
    _check_api_key(cfg)  # no raise


def test_api_key_empty_refused_in_webhook_dev_mode():
    """#518: WEBHOOK_DEV_MODE skips webhook signatures only. It must not keep an
    empty API_KEY bootable, which used to switch API auth off entirely."""
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(_env_file=None, auth_dev_mode=False, webhook_dev_mode=True, api_key="")
    with pytest.raises(RuntimeError, match="API_KEY must be set") as exc_info:
        _check_api_key(cfg)
    assert "WEBHOOK_DEV_MODE" in str(exc_info.value), "the refusal should say why the webhook flag does not help"


def test_api_key_required_in_production():
    """Production posture: both flags off, empty API_KEY → refuse to start."""
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(_env_file=None, auth_dev_mode=False, webhook_dev_mode=False, api_key="")
    with pytest.raises(RuntimeError, match="API_KEY must be set"):
        _check_api_key(cfg)


# ---------------------------------------------------------------------------
# _resolve_secrets_provider tests
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> Embry0Config:
    """Minimal Embry0Config for secrets-provider startup tests."""
    with patch.dict(os.environ, {}, clear=True):
        return Embry0Config(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestResolveSecretsProvider:
    def test_empty_key_prod_raises(self) -> None:
        """Production boot with empty key must raise RuntimeError."""
        cfg = _make_config(environment_secret_key="", auth_dev_mode=False)
        with pytest.raises(RuntimeError, match="ENVIRONMENT_SECRET_KEY must be set"):
            _resolve_secrets_provider(cfg)

    def test_default_key_prod_raises(self) -> None:
        """Production boot with the hardcoded example default must raise RuntimeError."""
        cfg = _make_config(environment_secret_key="embry0-dev-secret-key", auth_dev_mode=False)
        with pytest.raises(RuntimeError, match="insecure example default"):
            _resolve_secrets_provider(cfg)

    def test_empty_key_dev_mode_generates_ephemeral(self) -> None:
        """Dev-mode with empty key generates a functional ephemeral provider."""
        import asyncio

        cfg = _make_config(environment_secret_key="", auth_dev_mode=True)
        # Must not raise — returns a working ephemeral provider.
        provider = _resolve_secrets_provider(cfg)
        assert provider is not None
        # Provider must be functional end-to-end.
        ct = asyncio.run(provider.encrypt("hello"))
        pt = asyncio.run(provider.decrypt(ct))
        assert pt == "hello"
        # Two calls produce different ciphertexts (Fernet random IV).
        ct2 = asyncio.run(provider.encrypt("hello"))
        assert ct != ct2

    def test_default_key_dev_mode_generates_ephemeral(self) -> None:
        """Dev-mode with the insecure default key also generates an ephemeral provider."""
        cfg = _make_config(environment_secret_key="embry0-dev-secret-key", auth_dev_mode=True)
        provider = _resolve_secrets_provider(cfg)
        assert provider is not None

    def test_valid_key_returns_provider(self) -> None:
        """A strong custom key returns a working provider in production mode."""
        import asyncio

        cfg = _make_config(environment_secret_key="a-valid-strong-key-here", auth_dev_mode=False)
        provider = _resolve_secrets_provider(cfg)
        assert provider is not None
        ct = asyncio.run(provider.encrypt("secret-value"))
        pt = asyncio.run(provider.decrypt(ct))
        assert pt == "secret-value"


# ---------------------------------------------------------------------------
# Spec 1A §3.8 — unknown env keys are surfaced at startup
# ---------------------------------------------------------------------------


def test_unknown_prefixed_keys_are_logged_at_startup():
    """extra='ignore' drops a typo silently; the boot log must not."""
    with structlog.testing.capture_logs() as captured:
        _warn_unknown_env_vars({"GITLAB_POLL_INTERVAL": "60", "GITLAB_URL": "https://gitlab.com"})

    events = [e for e in captured if e["event"] == "unknown_env_keys"]
    assert events, f"expected an unknown_env_keys warning, got {[e['event'] for e in captured]}"
    assert events[0]["keys"] == ["GITLAB_POLL_INTERVAL"]
    assert events[0]["log_level"] == "warning"


def test_no_unknown_key_warning_for_a_clean_environment():
    with structlog.testing.capture_logs() as captured:
        _warn_unknown_env_vars({"GITHUB_TOKEN": "x", "DEFAULT_FORGE": "github"})

    assert [e for e in captured if e["event"] == "unknown_env_keys"] == []


def test_an_unknown_key_warns_but_never_refuses_startup():
    """A warning must stay a warning — only a malformed GITLAB_URL refuses."""
    with structlog.testing.capture_logs() as captured:
        _warn_unknown_env_vars({"EMBRY0_TYPOED_KNOB": "1"})

    assert [e["event"] for e in captured] == ["unknown_env_keys"]
