"""Migration 92 — jobs.interrupted_at / resume_attempts / launch (#307)."""

from embry0.storage.migrations.runner import MIGRATIONS


def test_migration_92_adds_the_recovery_columns() -> None:
    sql = next(sql for v, _, sql in MIGRATIONS if v == 92)
    for column in (
        "interrupted_at TIMESTAMPTZ",
        "resume_attempts INTEGER NOT NULL DEFAULT 0",
        "launch JSONB",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, f"migration 92 must add jobs.{column}"
