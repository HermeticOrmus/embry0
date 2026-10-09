"""What "the budget gate refused this job" means in SQL — the one definition (#331).

A refused job never ran: it must not count toward its actor's rate limit
(a lockout would extend itself) nor burn one of a ticket's dispatch attempts.
A job that ran, spent money and was hard-stopped in-run must count toward
both. Until #331 the two shared ``ERR_BUDGET_OVERRUN``; the rate limit
excluded both kinds, and the attempt cap told them apart by inference.

Now the gate writes its own code, ``ERR_BUDGET_REFUSED``, which says so
outright. Every other row — including every in-run hard stop written from now
on, which still carries ``ERR_BUDGET_OVERRUN`` — is judged by the evidence it
carries: a refusal has neither a ``traces`` row (one is written per agent run)
nor an overrun figure (the runner records ``budget_overrun_usd`` on an in-run
stop; a refusal has none). That second branch is what reads the rows written
before this code existed, and it is not dead for new ones. Requiring both
absences also covers an in-run stop whose best-effort trace write failed,
which the trace test alone would have read as a refusal.

Every reader embeds :func:`gate_refusal_sql`; none restates it.
"""

from __future__ import annotations

import re

from embry0.safety.error_codes import ErrorCode

_ALIAS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def gate_refusal_sql(jobs_alias: str) -> str:
    """Boolean SQL: the ``jobs`` row under ``jobs_alias`` was refused at the gate.

    ``jobs_alias`` is the table name or alias the caller's query uses for
    ``jobs``. It is the only thing interpolated, so it is refused unless it is
    a plain identifier; the codes come from :class:`ErrorCode`.
    """
    if not _ALIAS.fullmatch(jobs_alias):
        raise ValueError(f"jobs_alias must be a plain SQL identifier, got {jobs_alias!r}")
    j = jobs_alias
    # IS NOT DISTINCT FROM, not "=": error_code is NULL on most rows, and a
    # NULL result would drop the row from a caller's ``NOT (...)`` filter.
    return (
        f"({j}.error_code IS NOT DISTINCT FROM '{ErrorCode.BUDGET_REFUSED.value}'"
        f" OR ({j}.error_code IS NOT DISTINCT FROM '{ErrorCode.BUDGET_OVERRUN.value}'"
        f" AND {j}.budget_overrun_usd = 0"
        f" AND NOT EXISTS (SELECT 1 FROM traces gr_t WHERE gr_t.job_id = {j}.job_id)))"
    )
