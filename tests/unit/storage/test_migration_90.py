"""Migration 90 — drop qa_volume_state rows of per-job volumes whose job has ended (#328)."""

import re

import pytest

from embry0.cache.volume_ownership import _RELEASED_STATUSES
from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1, f"migration {version} must exist exactly once"
    return match[0][2]


def test_migration_90_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 90 in versions, "migration 90 (qa_volume_state cleanup, #328) is missing"
    assert versions == list(range(1, len(versions) + 1))


def test_the_terminal_statuses_match_the_volume_reaper() -> None:
    """A row may go exactly when the reaper would remove its volume."""
    listed = set(re.findall(r"'([a-z_]+)'", _sql_for(90).split("NOT IN", 1)[1]))
    assert listed == {str(s) for s in _RELEASED_STATUSES}, (
        f"migration 90 lists {sorted(listed)}, the reaper releases {sorted(map(str, _RELEASED_STATUSES))}"
    )


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_rows_of_ended_or_unknown_jobs_go_and_the_rest_stay(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    await db.execute("INSERT INTO repos (repo) VALUES ('acme/app') ON CONFLICT DO NOTHING")
    for job_id, status in (("job-done", "completed"), ("job-cancelled", "cancelled"), ("job-live", "running")):
        await db.execute(
            "INSERT INTO jobs (job_id, repo, task, status) VALUES ($1, 'acme/app', 't', $2)", job_id, status
        )
    rows = [
        ("per-job", "job-done"),
        ("per-job", "job-cancelled"),
        ("per-job", "job-live"),
        ("per-job", "job-never-recorded"),
        ("per-repo", "acme/app"),
    ]
    for scope, key in rows:
        await db.execute(
            "INSERT INTO qa_volume_state (scope, scope_key, volume_name) VALUES ($1, $2, $3)",
            scope,
            key,
            f"embry0-qa-vol-{key.replace('/', '-')}",
        )

    for _ in range(2):  # idempotent
        await db.execute(_sql_for(90))

    left = {(r["scope"], r["scope_key"]) for r in await db.fetch("SELECT scope, scope_key FROM qa_volume_state")}
    assert left == {("per-job", "job-live"), ("per-repo", "acme/app")}, f"rows left after migration 90: {left}"
