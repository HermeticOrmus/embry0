"""Budget tracking — checks and enforces spending limits.

Layer 1 of the three cost-protection layers (``docs/architecture.md``): the
per-job cap. Layers 2 and 3 — the daily/monthly rollup caps and the
per-author hourly rate limit — live in ``embry0.orchestration.budget_gate``
and run once, at dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Fallback per-job cap for state that carries neither a job-create override
# nor a triage-resolved ``pipeline_config.budget_usd``. Mirrors
# ``PipelineConfig.budget_usd``'s own default.
DEFAULT_JOB_BUDGET_USD = 10.0


class BudgetOverrunError(RuntimeError):
    """A job crossed its per-job cap while ``overrun_mode`` is ``hard``.

    Raised from the agent node so the run aborts at the ceiling instead of
    finishing the graph; ``error_code_for_exception`` maps it to
    ``ErrorCode.BUDGET_OVERRUN``.
    """

    def __init__(self, current_cost: float, max_budget: float) -> None:
        super().__init__(
            f"Job budget exhausted: ${current_cost:.2f} spent against a ${max_budget:.2f} cap (overrun_mode=hard)"
        )
        self.current_cost = current_cost
        self.max_budget = max_budget


@dataclass
class BudgetCheckResult:
    allowed: bool
    remaining: float
    overrun: float


def check_budget(
    current_cost: float,
    max_budget: float,
    overrun_mode: str = "soft",
) -> BudgetCheckResult:
    overrun = max(0.0, current_cost - max_budget)
    remaining = max(0.0, max_budget - current_cost)
    if overrun > 0:
        allowed = overrun_mode == "soft"
    else:
        allowed = True
    return BudgetCheckResult(allowed=allowed, remaining=remaining, overrun=overrun)


def effective_overrun_mode(fleet_mode: str, job_mode: Any) -> str:
    """The overrun mode for one job: a job may tighten the fleet mode, never loosen it.

    Unattended jobs (the watcher, #275) pin themselves to ``hard`` so their cap
    stops the run even while the fleet runs ``soft``. Any other job-level value
    is ignored — a job-create field must not be a way around an operator's
    ``hard``.
    """
    return "hard" if job_mode == "hard" else fleet_mode


def resolve_job_cap(state: dict[str, Any], *, ceiling: float = 0.0) -> float:
    """The per-job cap in force for ``state``.

    Triage folds a job-create ``max_budget_usd`` over ``budget_usd`` when it
    writes ``pipeline_config``, so that key wins once it exists; before triage
    has run the raw override is the only signal. ``ceiling`` is the fleet-wide
    ``budget_config.max_budget_per_job_usd`` — it bounds the result so a
    per-job choice can go under the operator's cap but never over it. 0 (the
    default) means no ceiling.
    """
    pipeline = state.get("pipeline_config") or {}
    if isinstance(pipeline, dict) and pipeline.get("budget_usd") is not None:
        cap = float(pipeline["budget_usd"])
    elif state.get("max_budget_usd") is not None:
        cap = float(state["max_budget_usd"])
    else:
        cap = DEFAULT_JOB_BUDGET_USD
    return min(cap, ceiling) if ceiling > 0 else cap
