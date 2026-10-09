"""Per-model enablement checks (RAV-1297).

The enablement set lives on the single ``provider_config`` row as its
inverse (``disabled_models``). Documented behaviour for a disabled model:

- **Save-time (agents, job overrides): reject.** An operator explicitly
  picking a disabled model gets a clear 422, not a silent substitution.
- **Runtime (a pinned agent row or template referencing a model disabled
  AFTER it was saved): fall back to the tier default with a warning** —
  a config change must never strand existing agents or kill jobs. See
  ``embry0.agents.resolver.resolve_agent_invocation``.

Tier-assigned models can never be disabled (PUT /config/provider
interlock), so the fallback target is always runnable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class DisabledModelError(ValueError):
    """An explicitly-selected model is disabled in provider settings."""


async def get_disabled_models(provider_repo: Any) -> set[str]:
    cfg = await provider_repo.get()
    return set(cfg.get("disabled_models") or [])


async def ensure_models_enabled(models: Iterable[str], provider_repo: Any) -> None:
    """Raise DisabledModelError if any of ``models`` is disabled."""
    wanted = [m for m in models if m]
    if not wanted:
        return
    disabled = await get_disabled_models(provider_repo)
    rejected = sorted(set(wanted) & disabled)
    if rejected:
        raise DisabledModelError(
            f"model(s) disabled in provider settings: {', '.join(rejected)} — "
            "re-enable under Settings → LLM Providers or pick an enabled model"
        )
