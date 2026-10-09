"""The combined verification result of one job (#298 + #299).

One JSON-able dict, stored in ``job_verifications.report`` and on
``JobState["verification"]``. ``human_merge_only`` is the single bit
auto-merge (#310) must honour: True means embry0 did not verify this change
well enough for a machine to merge it; ``human_merge_reasons`` says why.
"""

from __future__ import annotations

from typing import Any

from embry0.workflows.verification.gate import GateReport
from embry0.workflows.verification.tiers import BrowserQAPlan, TierDecision

SCHEMA_VERSION = 1
HUMAN_MERGE_LABEL = "human-merge-only"


def human_merge_reasons(tier: TierDecision, gate: GateReport, plan: BrowserQAPlan) -> list[str]:
    reasons: list[str] = []
    if plan.human_merge_only:
        reasons.append(plan.reason_code)
    if gate.verdict in ("new_failures", "error"):
        reasons.append(f"gate_{gate.verdict}")
    elif gate.verdict in ("not_configured", "no_matching_targets") and tier.tier != "neutral_only":
        # Code changed and no deterministic check covered it.
        reasons.append(f"gate_{gate.verdict}")
    elif any(t.status == "preexisting_failures" for t in gate.targets):
        # Base is red. Only a TEST-granular comparison could show that nothing
        # new broke, and even then the reading rests on a parser: a file-level
        # comparison cannot see inside an already-red file, and exit codes
        # cannot see at all. Flag every red base for a human rather than let
        # the coarse cases pass silently (review of #341).
        reasons.append("gate_base_red_unverified")
    return reasons


def compose_result(
    *,
    mode: str,
    tier: TierDecision,
    gate: GateReport,
    plan: BrowserQAPlan,
    config_error: str | None = None,
) -> dict[str, Any]:
    reasons = human_merge_reasons(tier, gate, plan)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "tier": tier.to_dict(),
        "gate": gate.to_dict(),
        "browser_qa": {"planned": "run" if plan.run_browser_qa else "skipped", "reason_code": plan.reason_code},
        "blocking": gate.blocking,
        "human_merge_only": bool(reasons),
        "human_merge_reasons": reasons,
        "config_error": config_error,
    }
