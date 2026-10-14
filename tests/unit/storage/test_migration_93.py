"""Migration 93 — drop qa_app_results.trace_url, which nothing ever set (#644)."""

import dataclasses

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS
from embry0.storage.repositories.qa_app_results import QAAppResultRow
from embry0.workflows.qa.subtask_result_schema import SubTaskResult


def _sql_for(version: int) -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == version]
    assert len(match) == 1, f"migration {version} must exist exactly once"
    return match[0]


def test_migration_93_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 93 in versions, "migration 93 (drop qa_app_results.trace_url, #644) is missing"
    assert versions == list(range(1, len(versions) + 1))


def test_no_qa_result_type_carries_trace_url() -> None:
    for cls in (SubTaskResult, QAAppResultRow):
        names = {f.name for f in dataclasses.fields(cls)}
        assert "trace_url" not in names, f"{cls.__name__} still carries the never-set trace_url"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_the_column_is_gone_and_the_migration_is_idempotent(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    await db.execute(_sql_for(93))  # a second run must be a no-op
    columns = {
        r["column_name"]
        for r in await db.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'qa_app_results'"
        )
    }
    assert "trace_url" not in columns, f"qa_app_results still has trace_url: {sorted(columns)}"
    assert {"failure_summary", "raw_result", "boot_phase"} <= columns, f"a kept column went missing: {sorted(columns)}"
