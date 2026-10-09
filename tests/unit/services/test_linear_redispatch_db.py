"""Linear re-dispatch against the real repositories (#284).

The fakes in test_linear_sync.py pin the decision logic; this pins what they
cannot — the ``open -> triaging`` compare-and-swap, ``jobs.list_all`` ordering
and the notice ledger, all on Postgres. Linear itself is never called.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from embry0.services.linear_sync import LinearSyncService, LinearTriggerIssue
from embry0.storage.repositories.dispatch_ledger import DispatchLedgerRepository
from embry0.storage.repositories.issues import IssuesRepository
from embry0.storage.repositories.jobs import JobsRepository
from embry0.trackers import TriggerContext

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

_TICKET = LinearTriggerIssue(
    tracker="linear",
    native_id="uuid-1",
    key="ENG-1",
    title="Fix it",
    body="",
    url="https://linear.app/x/issue/ENG-1",
    labels=["embry0"],
    project="Proj",
    team_key="ENG",
    project_summary="repo: o/r",
    state_type="backlog",
)
_CREATE = {"type": "Issue", "action": "create", "data": {"id": "uuid-1", "identifier": "ENG-1"}}
_RELABEL = {
    "type": "Issue",
    "action": "update",
    "data": {
        "id": "uuid-1",
        "identifier": "ENG-1",
        "labelIds": ["lbl-e"],
        "labels": [{"id": "lbl-e", "name": "embry0"}],
    },
    "updatedFrom": {"labelIds": []},
}


class _Executor:
    """Creates the pending job row the way ``IssueExecutor.execute`` does, runs nothing."""

    def __init__(self, jobs: JobsRepository) -> None:
        self._jobs = jobs

    async def execute(self, issue_id: str, **kw: Any) -> str:
        job_id: str = await self._jobs.create(
            repo="o/r", task="t", issue_id=issue_id, dispatched_by=kw.get("dispatched_by")
        )
        return job_id


async def test_relabel_after_failure_redispatches_and_a_live_job_dedupes(db_with_migrations) -> None:
    issues, jobs = IssuesRepository(db_with_migrations), JobsRepository(db_with_migrations)
    svc = LinearSyncService("lin_api_key", {})
    svc.fetch_trigger_issue = AsyncMock(return_value=_TICKET)  # type: ignore[method-assign]
    comments: list[str] = []

    async def _post(issue_id: str, body: str) -> bool:
        comments.append(body)
        return True

    svc.post_comment = _post  # type: ignore[method-assign]
    ctx = TriggerContext(
        issues_repo=issues,
        issue_executor=_Executor(jobs),
        jobs_repo=jobs,
        dispatch_ledger=DispatchLedgerRepository(db_with_migrations),
        trigger_labels={"embry0"},
    )

    first = await svc.handle_webhook_event(_CREATE, ctx)
    assert first["action"] == "dispatched"

    # Relabel while the first job is still pending: dedupe, stated once.
    for _ in range(2):
        live = await svc.handle_webhook_event(_RELABEL, ctx)
        assert live["status"] == "ignored" and live["dedupe"] == "live_job"
    assert sum("Did not start a second job" in c for c in comments) == 1

    # The job fails; the executor's terminal path returns the issue to open.
    await jobs.update(first["job_id"], status="running")
    await jobs.update(first["job_id"], status="failed")
    await issues.update(first["issue_id"], status="open")

    retry = await svc.handle_webhook_event(_RELABEL, ctx)

    assert retry["action"] == "dispatched" and retry["issue_id"] == first["issue_id"]
    assert retry["job_id"] != first["job_id"]
    rows, total = await jobs.list_all(issue_id=first["issue_id"])
    assert total == 2 and [r["status"] for r in rows] == ["pending", "failed"]
    assert (await issues.get(first["issue_id"]))["status"] == "triaging"


async def test_reconciler_bounds_hold_on_real_tables(db_with_migrations) -> None:
    """`on` mode end to end: the per-ticket and daily caps read what the dispatch path wrote."""
    from dataclasses import replace

    from embry0.services.linear_reconciler import ACTOR, LinearDispatchReconciler, ReconcilerSettings
    from embry0.storage.repositories.dispatch_ledger import DispatchLedgerRepository as Ledger

    issues, jobs = IssuesRepository(db_with_migrations), JobsRepository(db_with_migrations)
    tickets = [replace(_TICKET, key=f"ENG-{n}", native_id=f"uuid-{n}") for n in (1, 2, 3)]
    svc = LinearSyncService("lin_api_key", {})
    svc.list_trigger_issues = AsyncMock(return_value=tickets)  # type: ignore[method-assign]
    svc.trigger_label_added_since = AsyncMock(return_value=True)  # type: ignore[method-assign]
    svc.post_comment = AsyncMock(return_value=True)  # type: ignore[method-assign]
    reconciler = LinearDispatchReconciler(
        resolve_sync=AsyncMock(return_value=svc),
        resolve_trigger_labels=AsyncMock(return_value={"embry0"}),
        make_context=lambda labels: TriggerContext(
            issues_repo=issues,
            issue_executor=_Executor(jobs),
            jobs_repo=jobs,
            dispatch_ledger=Ledger(db_with_migrations),
            trigger_labels=labels,
            actor=ACTOR,
        ),
        settings=ReconcilerSettings(mode="on", max_dispatches_per_day=2, max_attempts_per_ticket=1),
    )

    first = await reconciler.run_once()
    second = await reconciler.run_once()

    assert first["dispatched"] == ["ENG-1", "ENG-2"]  # the daily cap of 2 stops RAV-3
    assert second["dispatched"] == []
    assert await jobs.count_dispatched_by(ACTOR) == 2

    # RAV-1's job fails and the label is re-applied: one attempt per ticket was the limit.
    (row,) = [
        r
        for r in (await jobs.list_all())[0]
        if r["issue_id"] == (await issues.get_by_tracker_ref("linear", None, "ENG-1"))["id"]
    ]
    await jobs.update(row["job_id"], status="running")
    await jobs.update(row["job_id"], status="failed")
    await issues.update(row["issue_id"], status="open")
    await db_with_migrations.execute("UPDATE jobs SET created_at = NOW() - INTERVAL '2 days'")  # free the daily cap

    third = await reconciler.run_once()

    assert "ENG-1" not in third["dispatched"] and third["dispatched"] == ["ENG-3"]
