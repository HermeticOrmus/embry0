"""Guards for migration 81 — unwrap double-encoded JSONB (#382).

Held the "is the last entry" baton until migration 82 took it.
"""

from __future__ import annotations

import json

import pytest

from embry0.storage.migrations.runner import MIGRATIONS

VERSION = 81


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == VERSION]
    assert len(match) == 1, f"migration {VERSION} is missing or duplicated"
    return match[0]


def test_migration_is_present_exactly_once() -> None:
    """Was "is the last entry" until 82 (test_migration_watcher_stack_scope.py)."""
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions.count(VERSION) == 1
    assert max(versions) >= VERSION


def test_it_only_touches_string_values() -> None:
    sql = _sql()
    assert "WHERE jsonb_typeof(payload) = 'string'" in sql
    assert "WHERE jsonb_typeof(sample_lines) = 'string'" in sql


@pytest.mark.requires_postgres
async def test_it_unwraps_both_tables_and_is_re_runnable(db_with_migrations) -> None:
    db = db_with_migrations
    await db.execute(
        "INSERT INTO digest_deliveries (day, payload) VALUES ('2026-09-21', to_jsonb($1::text)), ('2026-09-20', NULL)",
        json.dumps({"day": "2026-09-21", "jobs": {"completed": 2}}),
    )
    await db.execute(
        "INSERT INTO log_signatures (signature_id, template, first_seen_at, last_seen_at, sample_lines) "
        "VALUES ('sig1', 't', NOW(), NOW(), to_jsonb($1::text))",
        json.dumps(["ERROR boom 0"]),
    )
    for _ in range(2):  # the second pass must change nothing
        await db.execute(_sql())
        assert (
            await db.fetchval("SELECT payload->'jobs'->>'completed' FROM digest_deliveries WHERE day = '2026-09-21'")
            == "2"
        )
        assert await db.fetchval("SELECT payload FROM digest_deliveries WHERE day = '2026-09-20'") is None
        assert (
            await db.fetchval("SELECT sample_lines->>0 FROM log_signatures WHERE signature_id = 'sig1'")
            == "ERROR boom 0"
        )
