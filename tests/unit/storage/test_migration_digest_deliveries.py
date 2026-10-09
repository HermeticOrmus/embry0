"""Guards for migration 77 — digest_deliveries (the daily embry0 summary).

Held the "is the last entry" baton until migration 78 (merge_approvals) took
it; the version-agnostic uniqueness and contiguity guards stay in
test_migration_log_signatures.py.
"""

from __future__ import annotations

from embry0.storage.migrations.runner import MIGRATIONS

VERSION = 77


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == VERSION]
    assert len(match) == 1
    return match[0]


def test_migration_is_present_exactly_once() -> None:
    """Was "is the last entry" until 78; that assertion moved to
    test_migration_merge_approvals.py. This version still exists once."""
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions.count(VERSION) == 1
    assert max(versions) >= VERSION


def test_the_day_is_the_primary_key() -> None:
    """The claim-once send depends on it: ON CONFLICT needs a unique day."""
    assert "day        DATE PRIMARY KEY" in _sql()


def test_it_is_idempotent() -> None:
    assert "CREATE TABLE IF NOT EXISTS digest_deliveries" in _sql()
