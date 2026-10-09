"""Fixtures for the healing-case service tests (real Postgres)."""

from datetime import UTC, datetime, timedelta

import pytest

from embry0.services.healing.service import HealingCaseService
from embry0.storage.database import DatabasePool
from embry0.storage.repositories.healing_cases import HealingCasesRepository
from embry0.storage.repositories.issues import IssuesRepository
from embry0.storage.repositories.jobs import JobsRepository
from embry0.storage.repositories.watcher_runs import WatcherRunsRepository

REPO = "acme/widgets"


@pytest.fixture
def cases_repo(db_with_migrations: DatabasePool) -> HealingCasesRepository:
    return HealingCasesRepository(db_with_migrations)


@pytest.fixture
def service(cases_repo: HealingCasesRepository) -> HealingCaseService:
    return HealingCaseService(cases_repo)


@pytest.fixture
def seed(db_with_migrations: DatabasePool) -> "Seed":
    return Seed(db_with_migrations)


class Seed:
    """Creates the rows a case links to."""

    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    async def watcher_run(self, identifier: str | None = "ENG-1", title: str = "Fix the thing") -> str:
        now = datetime.now(UTC)
        return await WatcherRunsRepository(self.db).create(
            window_start=now - timedelta(hours=1),
            window_end=now,
            action="proposed",
            detail=title,
            fingerprint=f"fp-{title}",
            tracker="linear",
            tracker_key=identifier,
        )

    async def issue(self, title: str = "[ENG-1] [watcher] Fix the thing") -> str:
        return await IssuesRepository(self.db).create(title=title, repo=REPO)

    async def job(self, issue_id: str | None = None) -> str:
        return await JobsRepository(self.db).create(repo=REPO, task="fix", issue_id=issue_id)
