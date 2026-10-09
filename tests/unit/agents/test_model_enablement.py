"""RAV-1297 — provider catalog completeness + per-model enablement.

Covers the three layers: the registry split (catalog vs env-overlay), the
save-time rejection service, and the resolver's runtime tier fallback for
models disabled after an agent row pinned them.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.agents.providers import (
    ANTHROPIC,
    CATALOG_PROVIDERS,
    PROVIDERS,
    XAI,
    known_models,
    provider_for_model,
)
from embry0.agents.resolver import resolve_agent_invocation
from embry0.services.model_enablement import DisabledModelError, ensure_models_enabled

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_anthropic_is_in_catalog_but_not_overlay_registry():
    # The catalog answers "which providers do we have?"; PROVIDERS remains
    # the env-overlay registry — adding Anthropic there would make the
    # executor overlay Anthropic auth onto Anthropic runs.
    assert ANTHROPIC in CATALOG_PROVIDERS
    assert XAI in CATALOG_PROVIDERS
    assert ANTHROPIC not in PROVIDERS


def test_anthropic_catalog_shape():
    assert ANTHROPIC.base_url == "https://api.anthropic.com"
    assert set(ANTHROPIC.models) == {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"}
    assert ANTHROPIC.pricing_usd_per_mtok["claude-opus-5"] == (5.0, 25.0)


def test_provider_for_model_still_returns_none_for_claude():
    # The overlay contract must survive the catalog addition.
    assert provider_for_model("claude-opus-5") is None
    assert provider_for_model("grok-4.5") is XAI


def test_known_models_spans_all_catalog_providers():
    models = known_models()
    assert "claude-sonnet-5" in models
    assert "grok-4.5" in models


# ---------------------------------------------------------------------------
# Save-time rejection service
# ---------------------------------------------------------------------------


def _provider_repo(disabled: list[str]) -> MagicMock:
    repo = MagicMock()
    repo.get = AsyncMock(return_value={"disabled_models": disabled})
    return repo


@pytest.mark.asyncio
async def test_ensure_models_enabled_passes_for_enabled_models():
    await ensure_models_enabled(["claude-opus-5"], _provider_repo(["grok-4.5"]))


@pytest.mark.asyncio
async def test_ensure_models_enabled_rejects_disabled_model():
    with pytest.raises(DisabledModelError, match="grok-4.5"):
        await ensure_models_enabled(["grok-4.5"], _provider_repo(["grok-4.5"]))


@pytest.mark.asyncio
async def test_ensure_models_enabled_skips_db_read_for_empty_input():
    repo = _provider_repo([])
    await ensure_models_enabled([], repo)
    await ensure_models_enabled([""], repo)
    repo.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# Resolver runtime fallback
# ---------------------------------------------------------------------------

_TIERS = {
    "model_heavy": "claude-opus-5",
    "model_medium": "claude-sonnet-5",
    "model_light": "claude-haiku-4-5",
}


def _resolve(agent_type: str, model: str, model_policy=None):
    return resolve_agent_invocation(
        agent_type=agent_type,
        prompt="p",
        system_context="",
        global_defaults={"execution_mode": "sdk", "auth_mode": "oauth"},
        repo_prefs=None,
        job_overrides={},
        agent_definition={
            "model": model,
            "tools": [],
            "skills": [],
            "system_prompt": "",
            "mcp_servers": {},
            "execution_mode": None,
            "auth_mode": None,
        },
        pipeline_config={},
        max_turns=5,
        timeout_seconds=60,
        credentials={"api_key": "", "oauth_token": "tok"},
        model_policy=model_policy,
    )


def test_resolver_without_policy_keeps_pinned_model():
    assert _resolve("research", "grok-4.5").model == "grok-4.5"


def test_resolver_disabled_model_falls_back_to_medium_tier():
    inv = _resolve(
        "research",
        "grok-4.5",
        model_policy={"disabled_models": ["grok-4.5"], "tier_models": _TIERS},
    )
    assert inv.model == "claude-sonnet-5"
    # The provider tag must follow the substituted model — a grok agent
    # falling back to Claude must NOT keep the xai overlay.
    assert inv.provider is None


def test_resolver_developer_falls_back_to_heavy_tier():
    inv = _resolve(
        "developer",
        "claude-sonnet-4-6",
        model_policy={"disabled_models": ["claude-sonnet-4-6"], "tier_models": _TIERS},
    )
    assert inv.model == "claude-opus-5"


def test_resolver_keeps_pinned_model_when_fallback_unusable():
    # Fail-open: if the tier fallback is itself disabled (should be
    # impossible via the API interlock) or missing, run the pinned model.
    inv = _resolve(
        "research",
        "grok-4.5",
        model_policy={
            "disabled_models": ["grok-4.5", "claude-sonnet-5"],
            "tier_models": _TIERS,
        },
    )
    assert inv.model == "grok-4.5"
    assert inv.provider == "xai"


def test_resolver_enabled_model_unaffected_by_policy():
    inv = _resolve(
        "research",
        "claude-opus-5",
        model_policy={"disabled_models": ["grok-4.5"], "tier_models": _TIERS},
    )
    assert inv.model == "claude-opus-5"
