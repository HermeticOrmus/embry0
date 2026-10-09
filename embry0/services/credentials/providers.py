"""LLM providers the registry can resolve (spec 2026-09-29 §3.4).

The xAI refresh-token file is an importer input, not a runtime fallback.
Anthropic's runtime fallback is the OAuth token, then the credentials file.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# The path the orchestrator image reads today (SandboxManager._read_oauth_token).
_CLAUDE_CREDENTIALS_FILE = "/home/orchestrator/.claude/.credentials.json"


@dataclass(frozen=True)
class LlmProvider:
    name: str
    host: str
    kinds: tuple[str, ...]


LLM_PROVIDERS: dict[str, LlmProvider] = {
    "anthropic": LlmProvider("anthropic", "api.anthropic.com", ("llm_oauth", "llm_api_key")),
    "xai": LlmProvider("xai", "api.x.ai", ("llm_oauth", "llm_api_key")),
}


def default_host(provider: str) -> str:
    try:
        return LLM_PROVIDERS[provider].host
    except KeyError:
        raise ValueError(f"unknown llm provider {provider!r}") from None


def env_fallback(
    provider: str,
    environ: Mapping[str, str],
    config: Any,
    *,
    credentials_file: str | None = None,
) -> tuple[str, str, str] | None:
    """``(kind, secret, env_key)`` from the process environment, or None.

    ``config`` is accepted so a later fallback can read a path from settings.
    xAI's credential file is not read here.
    """
    del config
    if provider not in LLM_PROVIDERS:
        raise ValueError(f"unknown llm provider {provider!r}")
    if provider == "anthropic":
        token = (environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
        if token:
            return ("llm_oauth", token, "CLAUDE_CODE_OAUTH_TOKEN")
        file_token = _claude_access_token(credentials_file or _CLAUDE_CREDENTIALS_FILE)
        if file_token:
            return ("llm_oauth", file_token, "claude-credentials-file")
        return None
    key = (environ.get("XAI_API_KEY") or "").strip()
    return ("llm_api_key", key, "XAI_API_KEY") if key else None


def _claude_access_token(path: str) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as handle:
            raw = json.load(handle).get("claudeAiOauth", {}).get("accessToken")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    token = str(raw).strip() if raw else ""
    return token or None


def providers_for_job(
    agent_definitions: Any,
    agent_models_override: Mapping[str, str] | None,
    model_policy: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Providers a job must resolve before its sandbox exists.

    ``anthropic`` is always included: developer and reviewer default to Claude.
    ``provider_for_model`` returns None for a Claude model, which counts as
    anthropic. Disabled models are names the job must not run, so they do not
    pull a provider into the sandbox. The policy's ``default_model`` does:
    when set, it is the model most agents of the job resolve to (#465).
    """
    from embry0.agents.providers import provider_for_model

    models: list[str] = []
    if isinstance(agent_definitions, Mapping) and "model" not in agent_definitions:
        rows: Any = agent_definitions.values()
    elif isinstance(agent_definitions, Mapping):
        rows = [agent_definitions]
    else:
        rows = agent_definitions or []
    for row in rows:
        if isinstance(row, Mapping):
            model = row.get("model")
            if isinstance(model, str) and model.strip():
                models.append(model.strip())
    for model in (agent_models_override or {}).values():
        if isinstance(model, str) and model.strip():
            models.append(model.strip())
    default_model = (model_policy or {}).get("default_model")
    if isinstance(default_model, str) and default_model.strip():
        models.append(default_model.strip())
    tiers = (model_policy or {}).get("tier_models") or {}
    if isinstance(tiers, Mapping):
        for model in tiers.values():
            if isinstance(model, str) and model.strip():
                models.append(model.strip())
    names = {"anthropic"}
    for model in models:
        provider = provider_for_model(model)
        names.add(provider.name if provider is not None else "anthropic")
    return tuple(sorted(names))
