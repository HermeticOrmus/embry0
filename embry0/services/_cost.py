"""Authoritative per-job cost resolution, shared by every finalization path.

The workflow state's ``total_cost_usd`` is not the whole bill: the QA fan-out
runs as a sub-graph that never propagates per-agent cost up to the parent state
(only the issue→PR path accumulates it via ``run_agent_node``), so a job that
ran QA is short by the entire QA spend. Trace rows, by contrast, are written per
agent under the parent ``job_id`` on every path — and they are the same source
``GET /jobs/{id}`` derives ``cost_breakdown`` from, so using them keeps the job
row and the breakdown on the same page telling the same story.

This lives in one place because it has already drifted once: the standalone-job
path resolved cost from traces while the issue-backed path kept writing the raw
state value, and 46 jobs under-reported by $37.57 before anyone compared the two
numbers (RAV-1291).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from embry0.storage.repositories.traces import TracesRepository


async def resolve_job_total_cost(traces: TracesRepository | None, job_id: str, state_cost: float) -> float:
    """Return the job's true total: the trace sum, or *state_cost* if none exist.

    The fallback matters for a job that died before writing any trace row, and
    for any future path that does accumulate cost in state — neither should be
    zeroed out just because traces are empty.

    *traces* may be None: unit tests build IssueExecutor via ``__new__`` and set
    only the attributes their path touches, so callers pass
    ``getattr(self, "_traces", None)`` rather than making this a new
    construction requirement (same stance as ``_post_tracker_outcome``).
    """
    if traces is None:
        return state_cost
    traces_cost = await traces.total_cost_for_job(job_id)
    return traces_cost if traces_cost > 0.0 else state_cost
