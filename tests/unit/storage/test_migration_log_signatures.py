"""Guards for migration 76 — log_signatures (#289, SH-P3.1).

Also the current home of the **version-agnostic** contiguity guard. The
documented hazard is that the runner keys on the integer and silently SKIPS a
duplicate, so two PRs both claiming version N means one migration never runs and
nothing says so — it happened three times in one wave and four in the next.
``test_migration_38`` already pinned ordering; uniqueness and contiguity were
unguarded, which is the half that actually bites.

Those general assertions are written to never need editing again, so the next
migration only has to move the "is the last entry" baton.
"""

from __future__ import annotations

from embry0.storage.migrations.runner import MIGRATIONS

VERSION = 76


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == VERSION]
    assert len(match) == 1
    return match[0]


def test_versions_are_unique() -> None:
    """The guard the duplicate-version incidents actually needed.

    A repeated integer makes the runner skip a migration in silence — the failure
    mode is a missing table in production, discovered later and elsewhere.
    """
    versions = [v for v, _, _ in MIGRATIONS]
    duplicates = {v for v in versions if versions.count(v) > 1}
    assert not duplicates, f"duplicate migration versions: {sorted(duplicates)}"


def test_versions_are_contiguous_and_ordered() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions == sorted(versions), "MIGRATIONS must stay version-ordered"
    assert versions == list(range(1, len(versions) + 1)), "MIGRATIONS must be contiguous from 1"


def test_creates_the_table_with_a_composite_key_and_no_stored_rate() -> None:
    sql = _sql().upper()
    assert "CREATE TABLE IF NOT EXISTS LOG_SIGNATURES" in sql
    # repo belongs in the key: pooling one signature's rate across repos is
    # meaningless, and re-keying later is a painful migration.
    assert "PRIMARY KEY (SIGNATURE_ID, REPO)" in sql
    # No stored rate — it is stale the moment the next line lands, and #312
    # needs the rate before a fix shipped, which the counts already give.
    assert " RATE " not in sql and "RATE_PER_HOUR" not in sql


def test_migration_is_additive_only() -> None:
    """A new table must never take data with it if it is re-run or rolled back."""
    sql = _sql().upper()
    assert "DROP TABLE" not in sql
    assert "DELETE" not in sql
    assert "TRUNCATE" not in sql
    assert "ALTER TABLE" not in sql


def test_indexes_serve_the_two_reads_that_exist() -> None:
    sql = _sql()
    assert "idx_log_signatures_repo_last_seen" in sql
    assert "idx_log_signatures_repo_count" in sql
