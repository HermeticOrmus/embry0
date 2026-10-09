"""Job delivery — reads and the one write path behind ``jobs.delivery_state`` (#279, #280).

Kept apart from :class:`JobsRepository` on purpose: that class owns the job's
own lifecycle (``status``), this one owns what became of the job's pull
request. The rule deciding a write lives with its caller
(``services.merge_sync``); this module only guarantees the write is made
against the row as it is NOW — the row is locked while the caller's
``decide`` runs, so a webhook and a sweep recording the same PR at the same
moment cannot interleave.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog

from embry0.storage.database import DatabasePool
from embry0.storage.repositories.jobs import VALID_JOB_TRANSITIONS

logger = structlog.get_logger(__name__)

# Columns a matcher needs; `task`/`context`/`issue_id` re-derive the branch.
_COLUMNS = (
    "job_id, status, repo, task, issue_id, context, job_kind, pr_url, "
    "delivery_state, delivery_state_at, created_at, started_at"
)


@dataclass(frozen=True)
class DeliveryWrite:
    """Columns to set on one job; None leaves a column alone."""

    delivery_state: str | None = None
    delivery_state_at: datetime | None = None
    pr_url: str | None = None  # only ever FILLS an empty pr_url
    status: str | None = None


class JobDeliveryRepository:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    # ------------------------------------------------------------------ reads

    async def find_by_pr_urls(self, urls: Sequence[str]) -> list[dict[str, Any]]:
        """Jobs whose stored PR URL equals any of ``urls`` (case-insensitive,
        as GitHub slugs are), oldest first."""
        if not urls:
            return []
        rows = await self._db.fetch(
            f"SELECT {_COLUMNS} FROM jobs WHERE lower(pr_url) = ANY($1::text[]) ORDER BY created_at, job_id",
            [u.lower() for u in urls],
        )
        return [dict(r) for r in rows]

    async def find_unlinked_in_repo(
        self,
        repo_slugs: Sequence[str],
        *,
        created_before: datetime,
        window: timedelta,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Code jobs of a repo that have no PR URL yet — the candidates a PR
        opened from a job's branch by someone else can belong to: jobs created
        in the ``window`` before ``created_before`` (a job created after the PR
        was opened cannot have produced it), plus jobs dispatched onto an
        existing branch (``context.ref``), which by nature come after it."""
        if not repo_slugs:
            return []
        rows = await self._db.fetch(
            f"""
            SELECT {_COLUMNS} FROM jobs
            WHERE lower(repo) = ANY($1::text[])
              AND COALESCE(pr_url, '') = ''
              AND COALESCE(job_kind, 'code') = 'code'
              AND created_at >= $3 AND (created_at <= $2 OR context ? 'ref')
            ORDER BY created_at, job_id
            LIMIT $4
            """,
            [s.lower() for s in repo_slugs],
            created_before,
            created_before - window,
            limit,
        )
        return [dict(r) for r in rows]

    async def list_tracked(
        self,
        *,
        states: Sequence[str],
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Distinct stored PR URLs whose jobs are in one of ``states``, oldest
        job first. One row per URL: ``pr_url``, ``repo``, ``first_created``.

        Paged by keyset, not by offset: a sweep settles rows as it goes and a
        settled URL leaves this listing, so a numeric offset would skip the
        rows that shifted up into the space it left — the tail of a backlog
        larger than one sweep's budget would then never be reached.
        """
        args: list[Any] = [list(states), limit]
        # The cursor compares against the GROUPED key, so it is a HAVING.
        having = ""
        if after is not None:
            args += [after[0], after[1]]
            having = "HAVING (MIN(created_at), pr_url) > ($3, $4)"
        rows = await self._db.fetch(
            f"""
            SELECT pr_url, MIN(repo) AS repo, MIN(created_at) AS first_created
            FROM jobs
            WHERE COALESCE(pr_url, '') <> '' AND delivery_state = ANY($1::text[])
            GROUP BY pr_url
            {having}
            ORDER BY first_created, pr_url
            LIMIT $2
            """,
            *args,
        )
        return [dict(r) for r in rows]

    async def repos_with_unlinked_jobs(self, *, since: datetime) -> list[str]:
        """Repos that have a code job created after ``since`` with no PR URL —
        where a PR opened from a job's branch may still be waiting to be found."""
        rows = await self._db.fetch(
            """
            SELECT DISTINCT repo FROM jobs
            WHERE COALESCE(repo, '') <> '' AND COALESCE(pr_url, '') = ''
              AND COALESCE(job_kind, 'code') = 'code' AND created_at >= $1
            ORDER BY repo
            """,
            since,
        )
        return [r["repo"] for r in rows]

    async def misaligned_statuses(self) -> list[dict[str, Any]]:
        """Jobs whose delivery is settled but whose status has not followed —
        a job that was still running when its PR merged, and completed later."""
        rows = await self._db.fetch(
            f"""
            SELECT {_COLUMNS} FROM jobs
            WHERE (status = 'completed' AND delivery_state IN ('merged', 'closed'))
               OR (status = 'pr_closed' AND delivery_state IN ('merged', 'pr_open'))
            ORDER BY created_at, job_id
            """
        )
        return [dict(r) for r in rows]

    async def delivery_counts(self) -> dict[str, int]:
        rows = await self._db.fetch("SELECT delivery_state, COUNT(*) AS n FROM jobs GROUP BY delivery_state")
        return {r["delivery_state"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ write

    async def apply(
        self, job_id: str, decide: Callable[[dict[str, Any]], DeliveryWrite | None]
    ) -> tuple[dict[str, Any], DeliveryWrite] | None:
        """Lock the job, ask ``decide`` what to write given the row as it is
        now, write it. Returns ``(row before, write)`` or None when the job is
        gone or nothing was to be written.

        A ``status`` in the write must be a legal transition from the locked
        row's status (ValueError otherwise — a programmer error, since the row
        cannot move while it is locked).
        """
        async with self._db.transaction() as conn:
            record = await conn.fetchrow(f"SELECT {_COLUMNS} FROM jobs WHERE job_id = $1 FOR UPDATE", job_id)
            if record is None:
                return None
            row = dict(record)
            write = decide(row)
            if write is None:
                return None
            sets: list[str] = []
            args: list[Any] = [job_id]
            if write.delivery_state is not None:
                args += [write.delivery_state, write.delivery_state_at]
                sets += [f"delivery_state = ${len(args) - 1}", f"delivery_state_at = ${len(args)}"]
            if write.pr_url and not row["pr_url"]:
                args.append(write.pr_url)
                sets.append(f"pr_url = ${len(args)}")
            if write.status is not None and write.status != row["status"]:
                allowed = VALID_JOB_TRANSITIONS.get(row["status"], set())
                if write.status not in allowed:
                    raise ValueError(
                        f"Invalid job status transition: {row['status']} -> {write.status}. Allowed: {sorted(allowed)}"
                    )
                args.append(write.status)
                sets.append(f"status = ${len(args)}")
            if not sets:
                return None
            await conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = $1", *args)
            return row, write
