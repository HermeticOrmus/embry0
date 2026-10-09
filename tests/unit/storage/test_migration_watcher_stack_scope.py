"""Guards for migration 82 — watcher tables get a stack, the ticket gets a tracker.

Held the "is the last entry" baton until migration 83 took it.
"""

from __future__ import annotations

import pytest

from embry0.storage.migrations.runner import MIGRATIONS

VERSION = 82


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == VERSION]
    assert len(match) == 1, f"migration {VERSION} is missing or duplicated"
    return match[0]


def test_migration_is_present_exactly_once() -> None:
    """Was "is the last entry" until 83 (test_watcher_stacks_repo.py)."""
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions.count(VERSION) == 1
    assert max(versions) >= VERSION


def test_the_ticket_becomes_a_tracker_triple() -> None:
    sql = _sql()
    for column in ("tracker", "tracker_key", "tracker_url"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql
    assert "SET tracker = 'linear', tracker_key = linear_issue_identifier" in sql, "old rows must keep their ticket"
    assert "DROP COLUMN IF EXISTS linear_issue_identifier" in sql, "two sources of truth for one ticket"


def test_the_tick_mark_stops_being_one_row() -> None:
    """Two stacks sharing one anchor would each read the other's tick as theirs."""
    sql = _sql()
    assert "DROP CONSTRAINT IF EXISTS watcher_tick_attempts_id_check" in sql
    assert "ALTER TABLE watcher_tick_attempts ADD PRIMARY KEY (repo)" in sql


def test_pattern_memory_is_keyed_per_stack() -> None:
    assert "ALTER TABLE watcher_known_patterns ADD PRIMARY KEY (fingerprint, repo)" in _sql()


def test_the_open_case_index_is_repo_qualified() -> None:
    """GitHub/GitLab issue numbers repeat across repos; the old index did not."""
    sql = _sql()
    assert "ON healing_cases (repo, tracker, tracker_key)" in sql


@pytest.mark.requires_postgres
async def test_an_old_row_keeps_its_ticket_and_lands_unscoped(db_with_migrations) -> None:
    """The migration cannot know which stack a deployment watched, so rows stay
    at '' until the orchestrator adopts them (adopt_unscoped_rows)."""
    db = db_with_migrations
    await db.execute(
        "INSERT INTO watcher_runs (id, window_start, window_end, log_lines, action, tracker, tracker_key) "
        "VALUES ('wr-old', NOW(), NOW(), 5, 'proposed', 'linear', 'ENG-1')"
    )
    row = await db.fetchrow("SELECT repo, tracker, tracker_key FROM watcher_runs WHERE id = 'wr-old'")
    assert (row["repo"], row["tracker"], row["tracker_key"]) == ("", "linear", "ENG-1")


@pytest.mark.requires_postgres
async def test_two_repos_may_each_hold_issue_42(db_with_migrations) -> None:
    """The point of repo-qualifying the open-case index."""
    db = db_with_migrations
    for n, repo in enumerate(("owner/a", "owner/b")):
        await db.execute(
            "INSERT INTO healing_cases (id, repo, origin, title, stage, tracker, tracker_key) "
            "VALUES ($1, $2, 'watcher', 't', 'tracked', 'github', '42')",
            f"hc-{n}",
            repo,
        )
    assert await db.fetchval("SELECT count(*) FROM healing_cases WHERE tracker_key = '42'") == 2
