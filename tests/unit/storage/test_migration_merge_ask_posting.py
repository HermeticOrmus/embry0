"""Guards for migration 79 — asks post once CI settles and retire themselves.

Held the "is the last entry" baton until migration 80 took it; the
version-agnostic uniqueness and contiguity guards stay in
test_migration_log_signatures.py.
"""

from __future__ import annotations

from embry0.storage.migrations.runner import MIGRATIONS

VERSION = 79


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == VERSION]
    assert len(match) == 1, f"migration {VERSION} is missing or duplicated"
    return match[0]


def test_migration_is_present_exactly_once() -> None:
    """Was "is the last entry" until 80; that assertion moved to
    test_migration_merge_ask_jsonb.py. This version still exists once."""
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions.count(VERSION) == 1
    assert max(versions) >= VERSION


def test_rows_from_before_were_already_posted() -> None:
    """Without the backfill, the sweep would re-post every ask 78 already sent."""
    assert "UPDATE merge_approvals SET posted_at = created_at WHERE posted_at IS NULL" in _sql()


def test_the_two_retirement_states_are_allowed() -> None:
    assert (
        "CHECK (status IN ('pending', 'approved', 'rejected', 'merged', 'failed', 'stale', 'closed', 'expired'))"
        in _sql()
    )


def test_it_is_idempotent() -> None:
    sql = _sql()
    assert "ADD COLUMN IF NOT EXISTS posted_at" in sql
    assert "DROP CONSTRAINT IF EXISTS merge_approvals_status_check" in sql
