"""Two watched stacks share these tables without seeing each other (#378).

Every one of these reads was process-global before migration 82: the tick
watermark, the back-off, the daily analysis budget, dedupe, the briefing and
the draft cap. A second stack would have throttled and polluted the first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.repositories.watcher_patterns import WatcherPatternsRepository
from embry0.storage.repositories.watcher_runs import WatcherRunsRepository

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

A = "owner/stack-a"
B = "owner/stack-b"
T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


async def _run(repo: WatcherRunsRepository, stack: str, action: str, *, at: datetime = T0, **fields: object) -> str:
    return await repo.create(window_start=at - timedelta(hours=1), window_end=at, action=action, repo=stack, **fields)


async def test_the_window_watermark_is_per_stack(db_with_migrations: DatabasePool) -> None:
    """The next window starts where this stack's last analysed one ended; a
    busier stack must not drag it forward and lose logs."""
    repo = WatcherRunsRepository(db_with_migrations)
    await _run(repo, A, "no_issue", at=T0)
    await _run(repo, B, "no_issue", at=T0 + timedelta(hours=5))

    assert await repo.last_analysed_window_end(repo=A) == T0
    assert await repo.last_analysed_window_end(repo=B) == T0 + timedelta(hours=5)
    assert await repo.last_analysed_window_end() == T0 + timedelta(hours=5), "unscoped still spans every stack"


async def test_the_daily_analysis_budget_is_per_stack(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    now = datetime.now(UTC)
    for _ in range(3):
        await _run(repo, A, "proposed", at=now, job_id="j", analysed_under_full_cap=True)
    await _run(repo, B, "proposed", at=now, job_id="j", analysed_under_full_cap=True)

    assert await repo.full_cap_analyses_since(repo=A) == 3
    assert await repo.full_cap_analyses_since(repo=B) == 1


async def test_dedupe_and_the_briefing_do_not_cross_stacks(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    now = datetime.now(UTC)
    await _run(repo, A, "proposed", at=now, fingerprint="fp-shared", detail="A's finding", rationale="because A")

    assert await repo.fingerprint_exists("fp-shared", repo=A) is True
    assert await repo.fingerprint_exists("fp-shared", repo=B) is False, "B would skip its own first sighting"
    assert await repo.recent_proposal_titles(repo=B) == []
    assert await repo.recent_findings(repo=B) == []
    assert [r["detail"] for r in await repo.recent_findings(repo=A)] == ["A's finding"]


async def test_the_unparseable_backoff_is_per_stack(db_with_migrations: DatabasePool) -> None:
    """A stack whose analysis came back unparseable must not withhold another's."""
    repo = WatcherRunsRepository(db_with_migrations)
    now = datetime.now(UTC)
    await _run(repo, A, "error", at=now, detail="unparseable analysis output: no JSON", log_lines=7)
    await _run(repo, B, "no_issue", at=now)

    backoff = await repo.unparseable_backoff(repo=A)
    assert backoff is not None and backoff["log_lines"] == 7
    assert await repo.unparseable_backoff(repo=B) is None


async def test_the_draft_cap_counts_only_its_own_stack(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    now = datetime.now(UTC)
    await _run(repo, A, "proposed", at=now, tracker="linear", tracker_key="ENG-1")
    await _run(repo, B, "proposed", at=now, tracker="github", tracker_key="42")

    assert await repo.open_proposal_count(repo=A) == 1
    assert await repo.open_proposal_tickets(repo=B) == [{"tracker": "github", "key": "42"}]
    assert await repo.open_proposal_count() == 2


async def test_the_tick_mark_is_per_stack(db_with_migrations: DatabasePool) -> None:
    """Shared, a restart could read the other stack's tick and skip its own."""
    repo = WatcherRunsRepository(db_with_migrations)
    await repo.record_tick_attempt(T0, repo=A)
    await repo.record_tick_attempt(T0 + timedelta(hours=3), repo=B)

    assert await repo.last_tick_attempt_at(repo=A) == T0
    assert await repo.last_tick_attempt_at(repo=B) == T0 + timedelta(hours=3)

    await repo.record_tick_attempt(T0 - timedelta(hours=1), repo=A)
    assert await repo.last_tick_attempt_at(repo=A) == T0, "the mark only moves forward"


async def test_benign_pattern_memory_is_per_stack(db_with_migrations: DatabasePool) -> None:
    """Keyed on the fingerprint alone, B's note overwrote A's and both read it."""
    patterns = WatcherPatternsRepository(db_with_migrations)
    await patterns.upsert("fp-1", "harmless on A", repo=A)
    await patterns.upsert("fp-1", "means something else on B", repo=B)

    assert [(p["repo"], p["summary"]) for p in await patterns.list_recent(repo=A)] == [(A, "harmless on A")]
    assert [(p["repo"], p["summary"]) for p in await patterns.list_recent(repo=B)] == [(B, "means something else on B")]
    assert len(await patterns.list_recent()) == 2


async def test_pre_stack_rows_are_adopted_once(db_with_migrations: DatabasePool) -> None:
    """Migration 82 cannot know the stack; the orchestrator does, at startup."""
    db = db_with_migrations
    repo = WatcherRunsRepository(db)
    await db.execute(
        "INSERT INTO watcher_runs (id, window_start, window_end, log_lines, action) "
        "VALUES ('wr-legacy', NOW(), NOW(), 5, 'no_issue')"
    )
    await db.execute("INSERT INTO watcher_known_patterns (fingerprint, summary) VALUES ('fp-legacy', 's')")
    await db.execute("INSERT INTO watcher_tick_attempts (attempted_at) VALUES (NOW())")

    assert await repo.adopt_unscoped_rows(A) == 3
    assert await db.fetchval("SELECT repo FROM watcher_runs WHERE id = 'wr-legacy'") == A
    assert await db.fetchval("SELECT repo FROM watcher_known_patterns WHERE fingerprint = 'fp-legacy'") == A
    assert await db.fetchval("SELECT repo FROM watcher_tick_attempts") == A

    assert await repo.adopt_unscoped_rows(A) == 0, "a second startup must not re-claim"
    await _run(repo, B, "no_issue")
    assert await repo.adopt_unscoped_rows(B) == 0, "another stack must not steal them"
