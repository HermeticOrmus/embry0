"""Guards for migration 80 — unwrap double-encoded merge_approvals.ask.

Held the "is the last entry" baton until migration 81 took it.
"""

from __future__ import annotations

import json

import pytest

from embry0.storage.migrations.runner import MIGRATIONS

VERSION = 80


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == VERSION]
    assert len(match) == 1, f"migration {VERSION} is missing or duplicated"
    return match[0]


def test_migration_is_present_exactly_once() -> None:
    """Was "is the last entry" until 81 (test_migration_jsonb_unwrap_382.py)."""
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions.count(VERSION) == 1
    assert max(versions) >= VERSION


def test_it_only_touches_string_rows() -> None:
    assert "WHERE jsonb_typeof(ask) = 'string'" in _sql()


@pytest.mark.requires_postgres
async def test_it_turns_a_double_encoded_row_back_into_an_object(db_with_migrations) -> None:
    db = db_with_migrations
    await db.execute(
        "INSERT INTO merge_approvals (id, repo, pr_number, pr_url, head_sha, ask) "
        "VALUES ('mrg-legacy', 'o/r', 1, 'u', 'h', to_jsonb($1::text))",
        json.dumps({"title": "Fix it"}),
    )
    assert await db.fetchval("SELECT jsonb_typeof(ask) FROM merge_approvals WHERE id = 'mrg-legacy'") == "string"
    await db.execute(_sql())
    assert await db.fetchval("SELECT ask->>'title' FROM merge_approvals WHERE id = 'mrg-legacy'") == "Fix it"
    await db.execute(_sql())  # re-runnable: an object row is left alone
    assert await db.fetchval("SELECT ask->>'title' FROM merge_approvals WHERE id = 'mrg-legacy'") == "Fix it"
