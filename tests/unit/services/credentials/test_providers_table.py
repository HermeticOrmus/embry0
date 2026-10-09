"""LLM provider table: hosts and the env-fallback tuple (spec §3.4)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from embry0.services.credentials.providers import LLM_PROVIDERS, default_host, env_fallback


def test_provider_table_names_hosts_and_kinds():
    assert set(LLM_PROVIDERS) == {"anthropic", "xai"}
    assert default_host("anthropic") == "api.anthropic.com"
    assert default_host("xai") == "api.x.ai"
    assert LLM_PROVIDERS["anthropic"].kinds == ("llm_oauth", "llm_api_key")
    assert LLM_PROVIDERS["xai"].kinds == ("llm_oauth", "llm_api_key")


def test_env_fallback_anthropic_token_then_file(tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "file-token"}}))
    cfg = SimpleNamespace()
    assert env_fallback("anthropic", {"CLAUDE_CODE_OAUTH_TOKEN": "env-token"}, cfg, credentials_file=str(creds)) == (
        "llm_oauth",
        "env-token",
        "CLAUDE_CODE_OAUTH_TOKEN",
    )
    assert env_fallback("anthropic", {}, cfg, credentials_file=str(creds)) == (
        "llm_oauth",
        "file-token",
        "claude-credentials-file",
    )
    assert env_fallback("anthropic", {}, cfg, credentials_file=str(tmp_path / "missing.json")) is None


def test_env_fallback_xai_api_key_only():
    assert env_fallback("xai", {"XAI_API_KEY": "xai-key"}, SimpleNamespace()) == (
        "llm_api_key",
        "xai-key",
        "XAI_API_KEY",
    )
    assert env_fallback("xai", {}, SimpleNamespace()) is None


def test_unknown_provider_rejected():
    with pytest.raises(ValueError, match="openai"):
        default_host("openai")
