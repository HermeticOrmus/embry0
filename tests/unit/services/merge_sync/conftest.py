"""Fixtures for merge-sync tests (real Postgres, real healing ledger)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from embry0.services.healing.service import HealingCaseService
from embry0.services.merge_sync.recorder import DeliveryRecorder
from embry0.storage.database import DatabasePool
from embry0.storage.repositories.healing_cases import HealingCasesRepository
from embry0.storage.repositories.issues import IssuesRepository
from embry0.storage.repositories.job_delivery import JobDeliveryRepository
from embry0.storage.repositories.jobs import JobsRepository

REPO = "acme/widgets"
T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def pr_object(
    number: int = 7,
    *,
    repo: str = REPO,
    ref: str = "embry0/unknown-fix-the-thing",
    state: str = "open",
    created: datetime = T0,
    updated: datetime | None = None,
    closed: datetime | None = None,
    merged: datetime | None = None,
    head_repo: str | None = None,
) -> dict[str, Any]:
    """A GitHub pull-request object, as a webhook, a GET and a listing carry it."""
    ended = merged or closed
    return {
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "number": number,
        "state": "closed" if ended else state,
        "merged": merged is not None,
        "merged_at": iso(merged) if merged else None,
        "closed_at": iso(ended) if ended else None,
        "created_at": iso(created),
        "updated_at": iso(updated or ended or created),
        "merge_commit_sha": "m" * 40 if merged else "t" * 40,
        "head": {"ref": ref, "sha": "h" * 40, "repo": {"full_name": head_repo or repo}},
        "base": {"repo": {"full_name": repo}},
    }


class Seed:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db
        self.jobs = JobsRepository(db)
        self.issues = IssuesRepository(db)

    async def issue(self, title: str = "Fix the thing") -> str:
        return await self.issues.create(title=title, repo=REPO)

    async def job(
        self,
        *,
        status: str = "completed",
        pr_url: str | None = None,
        issue_id: str | None = None,
        task: str = "Fix the thing",
        repo: str = REPO,
        created: datetime = T0 - timedelta(hours=1),
        context: dict[str, Any] | None = None,
        job_kind: str | None = None,
    ) -> str:
        job_id = await self.jobs.create(repo=repo, task=task, issue_id=issue_id, context=context)
        # Written by SQL: tests need terminal statuses without walking the
        # lifecycle, and a creation time before the PR's.
        await self.db.execute(
            "UPDATE jobs SET status = $2, created_at = $3, job_kind = $4 WHERE job_id = $1",
            job_id,
            status,
            created,
            job_kind,
        )
        if pr_url:
            await self.jobs.update(job_id, pr_url=pr_url)
        return job_id

    async def row(self, job_id: str) -> dict[str, Any]:
        record = await self.db.fetchrow("SELECT * FROM jobs WHERE job_id = $1", job_id)
        assert record is not None
        return dict(record)

    async def delivery(self, job_id: str) -> tuple[str, str]:
        row = await self.row(job_id)
        return row["status"], row["delivery_state"]


@pytest.fixture
def seed(db_with_migrations: DatabasePool) -> Seed:
    return Seed(db_with_migrations)


@pytest.fixture
def healing(db_with_migrations: DatabasePool) -> HealingCaseService:
    return HealingCaseService(HealingCasesRepository(db_with_migrations))


@pytest.fixture
def delivery_repo(db_with_migrations: DatabasePool) -> JobDeliveryRepository:
    return JobDeliveryRepository(db_with_migrations)


@pytest.fixture
def recorder(
    db_with_migrations: DatabasePool, delivery_repo: JobDeliveryRepository, healing: HealingCaseService
) -> DeliveryRecorder:
    return DeliveryRecorder(delivery_repo, IssuesRepository(db_with_migrations), healing)
