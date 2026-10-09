"""``deferred`` watcher runs — a finding seen while the draft cap was full (#283)."""

from datetime import UTC, datetime, timedelta

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.repositories.watcher_runs import ANALYSED_ACTIONS, WatcherRunsRepository

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]


async def _run(repo: WatcherRunsRepository, action: str, **fields: object) -> str:
    now = datetime.now(UTC)
    return await repo.create(window_start=now - timedelta(hours=1), window_end=now, action=action, **fields)  # type: ignore[arg-type]


async def test_a_deferred_tick_was_analysed_so_the_window_moves_on(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    assert "deferred" in ANALYSED_ACTIONS
    await _run(repo, "deferred", detail="Later", fingerprint="fp-1")
    assert await repo.last_analysed_window_end() is not None


async def test_a_deferred_finding_is_known_to_dedup_and_to_the_briefing(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    await _run(repo, "deferred", detail="Later", fingerprint="fp-1", rationale="why")
    assert await repo.fingerprint_exists("fp-1", days=7)  # not deferred twice
    assert await repo.recent_proposal_titles(days=7) == ["Later"]  # nor re-proposed reworded
    assert [f["action"] for f in await repo.recent_findings(days=7)] == ["deferred"]
    # ... and it holds no slot of the old cap either.
    assert await repo.open_proposal_count(days=7) == 0


async def test_mark_filed_converts_the_deferred_row_once_and_keeps_its_age(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    run_id = await _run(repo, "deferred", detail="Later", fingerprint="fp-1")
    before = await repo.get(run_id)

    assert await repo.mark_filed(run_id, tracker="linear", key="ENG-7") is True
    after = await repo.get(run_id)
    assert (after["action"], after["tracker"], after["tracker_key"]) == ("proposed", "linear", "ENG-7")
    assert after["created_at"] == before["created_at"]
    assert await repo.open_proposal_tickets(days=7) == [{"tracker": "linear", "key": "ENG-7"}]

    assert await repo.mark_filed(run_id, tracker="linear", key="ENG-8") is False  # not deferred any more
    assert (await repo.get(run_id))["tracker_key"] == "ENG-7"


async def test_mark_filed_touches_only_deferred_rows(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    run_id = await _run(repo, "no_issue")
    assert await repo.mark_filed(run_id, tracker="linear", key="ENG-7") is False
    assert await repo.mark_filed("wr-missing", tracker="linear", key="ENG-7") is False
    assert (await repo.get(run_id))["action"] == "no_issue"
    assert await repo.get("wr-missing") is None


# ---- the full-cap analysis budget (#283, review of #340) ---------------------


async def test_the_spend_column_migration_is_idempotent() -> None:
    from embry0.storage.migrations.runner import MIGRATIONS

    (sql,) = [sql for v, _, sql in MIGRATIONS if v == 74]
    assert "ADD COLUMN IF NOT EXISTS analysed_under_full_cap" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_watcher_runs_full_cap_analyses" in sql
    # Additive with a default: no UPDATE over historical rows.
    assert "UPDATE watcher_runs" not in sql


async def test_rows_that_predate_the_column_do_not_count(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    await _run(repo, "no_issue", job_id="job-1")  # an ordinary analysed tick
    assert await repo.full_cap_analyses_since(hours=24) == 0


async def test_only_full_cap_analyses_count_and_a_retry_counts_twice(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    await _run(repo, "no_issue", job_id="job-1", analysed_under_full_cap=True)
    await _run(repo, "deferred", job_id="job-2", fingerprint="fp", analysed_under_full_cap=True)
    # A tick whose first analysis was unparseable paid for two jobs.
    await _run(repo, "error", job_id="job-4", retried_job_id="job-3", analysed_under_full_cap=True)
    # Not counted: an ordinary tick, and a full-cap tick that bought nothing.
    await _run(repo, "proposed", job_id="job-5", fingerprint="fp2", tracker="linear", tracker_key="ENG-1")
    await _run(repo, "skipped_analysis_cap", analysed_under_full_cap=False)

    assert await repo.full_cap_analyses_since(hours=24) == 4


async def test_the_budget_counts_a_rolling_window_not_all_of_history(db_with_migrations: DatabasePool) -> None:
    repo = WatcherRunsRepository(db_with_migrations)
    run_id = await _run(repo, "no_issue", job_id="job-old", analysed_under_full_cap=True)
    await db_with_migrations.execute(
        "UPDATE watcher_runs SET created_at = NOW() - interval '25 hours' WHERE id = $1", run_id
    )
    await _run(repo, "no_issue", job_id="job-new", analysed_under_full_cap=True)

    assert await repo.full_cap_analyses_since(hours=24) == 1
    assert await repo.full_cap_analyses_since(hours=48) == 2


async def test_the_budget_column_survives_a_restart(db_with_migrations: DatabasePool) -> None:
    # The point of counting from rows: a new process sees what the old one spent.
    await _run(WatcherRunsRepository(db_with_migrations), "no_issue", job_id="j", analysed_under_full_cap=True)
    assert await WatcherRunsRepository(db_with_migrations).full_cap_analyses_since(hours=24) == 1


async def test_the_skipped_analysis_cap_tick_did_not_reach_a_verdict(db_with_migrations: DatabasePool) -> None:
    # So it must not advance the query window: those logs are still unanalysed.
    repo = WatcherRunsRepository(db_with_migrations)
    assert "skipped_analysis_cap" not in ANALYSED_ACTIONS
    await _run(repo, "skipped_analysis_cap")
    assert await repo.last_analysed_window_end() is None
