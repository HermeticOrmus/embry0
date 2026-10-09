"""Guards for migration 78 — merge_approvals (the chat merge-approval loop).

Held the "is the last entry" baton until migration 79 took it; the
version-agnostic uniqueness and contiguity guards stay in
test_migration_log_signatures.py.
"""

from __future__ import annotations

from embry0.storage.migrations.runner import MIGRATIONS

VERSION = 78


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == VERSION]
    assert len(match) == 1, f"migration {VERSION} is missing or duplicated"
    return match[0]


def test_migration_is_present_exactly_once() -> None:
    """Was "is the last entry" until 79; that assertion moved to
    test_migration_merge_ask_posting.py. This version still exists once."""
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions.count(VERSION) == 1
    assert max(versions) >= VERSION


def test_one_pending_ask_per_pr() -> None:
    """A second ask for a PR that already waits on one must not duplicate it."""
    sql = _sql()
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_merge_approvals_one_pending_per_pr" in sql
    assert "ON merge_approvals (repo, pr_number) WHERE status = 'pending'" in sql


def test_status_vocabulary_is_closed() -> None:
    assert "CHECK (status IN ('pending', 'approved', 'rejected', 'merged', 'failed', 'stale'))" in _sql()


def test_dry_run_is_the_default() -> None:
    """A row written without an explicit mode records a dry run, never a merge."""
    assert "dry_run     BOOLEAN NOT NULL DEFAULT TRUE" in _sql()


def test_it_is_idempotent() -> None:
    assert "CREATE TABLE IF NOT EXISTS merge_approvals" in _sql()


def test_the_merge_key_scope_is_allowed_by_the_api_keys_check() -> None:
    """Without this the scope exists in code and every INSERT of one fails."""
    sql = _sql()
    assert "ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_scope_check" in sql
    assert "CHECK (scope IN ('read', 'operate', 'admin', 'merge'))" in sql
