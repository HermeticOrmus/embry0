"""WatcherRunsRepository dedup-window plumbing (RAV-1073).

Pure-mock tests: the age windows on ``open_proposal_count`` and
``fingerprint_exists`` are SQL clauses, so we assert the ``days`` bound is
threaded into the query rather than round-tripping a live Postgres (the
requires_postgres integration suite skips in the local gate).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.storage.repositories.watcher_runs import WatcherRunsRepository


def _repo(fetchrow_return):
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value=fetchrow_return)
    return WatcherRunsRepository(db), db


@pytest.mark.asyncio
async def test_open_proposal_count_windows_by_days():
    repo, db = _repo({"n": 2})
    n = await repo.open_proposal_count(days=3)
    assert n == 2
    sql, *args = db.fetchrow.await_args.args
    assert "make_interval(days => $1)" in sql
    assert args == [3, None]  # trailing None: every stack (#378)


@pytest.mark.asyncio
async def test_open_proposal_count_defaults_to_seven_days():
    repo, db = _repo({"n": 0})
    await repo.open_proposal_count()
    _, *args = db.fetchrow.await_args.args
    assert args == [7, None]


@pytest.mark.asyncio
async def test_open_proposal_count_none_row_is_zero():
    repo, db = _repo(None)
    assert await repo.open_proposal_count() == 0


@pytest.mark.asyncio
async def test_fingerprint_exists_windows_by_days():
    repo, db = _repo({"?column?": 1})
    seen = await repo.fingerprint_exists("fp-abc", days=5)
    assert seen is True
    sql, *args = db.fetchrow.await_args.args
    assert "make_interval(days => $2)" in sql
    assert args == ["fp-abc", 5, ["proposed", "deferred"], None]  # #283: a deferred finding dedups too


@pytest.mark.asyncio
async def test_fingerprint_exists_absent_within_window():
    repo, db = _repo(None)
    assert await repo.fingerprint_exists("fp-xyz") is False
    _, *args = db.fetchrow.await_args.args
    assert args == ["fp-xyz", 7, ["proposed", "deferred"], None]  # default window


# ---- briefing-pack additions (RAV-1106/1138) --------------------------------


def _repo_fetch(fetch_return):
    db = MagicMock()
    db.fetch = AsyncMock(return_value=fetch_return)
    db.execute = AsyncMock(return_value="UPDATE 2")
    return WatcherRunsRepository(db), db


@pytest.mark.asyncio
async def test_recent_findings_selects_rationale_within_window():
    repo, db = _repo_fetch(
        [{"action": "no_issue", "detail": None, "fingerprint": "fp", "rationale": "r", "created_at": None}]
    )
    rows = await repo.recent_findings(days=3, limit=5)
    assert rows[0]["rationale"] == "r"
    sql, *args = db.fetch.await_args.args
    assert "rationale" in sql
    assert "IN ('proposed', 'deferred', 'no_issue')" in sql
    assert "make_interval(days => $1)" in sql
    assert args == [3, 5, None]


@pytest.mark.asyncio
async def test_open_proposal_tickets_mirrors_cap_predicate():
    repo, db = _repo_fetch(
        [{"tracker": "linear", "tracker_key": "ENG-1"}, {"tracker": "linear", "tracker_key": "ENG-2"}]
    )
    ids = [t["key"] for t in await repo.open_proposal_tickets(days=7)]
    assert ids == ["ENG-1", "ENG-2"]
    sql, *args = db.fetch.await_args.args
    # Must match open_proposal_count's predicate exactly — what gets
    # reconciled is what pins the cap.
    assert "action = 'proposed'" in sql
    assert "NOT EXISTS" in sql
    assert "released_at IS NULL" in sql
    assert "make_interval(days => $1)" in sql
    assert args == [7, None]


@pytest.mark.asyncio
async def test_release_ticket_stamps_and_reports_rows():
    repo, db = _repo_fetch([])
    released = await repo.release_ticket("linear", "ENG-1097")
    assert released == 2
    sql, *args = db.execute.await_args.args
    # #278: the slot is released by a stamp; the ticket id is never cleared.
    assert "SET released_at = NOW()" in sql
    assert "tracker_key = NULL" not in sql
    assert args == ["ENG-1097", "linear"]


@pytest.mark.asyncio
async def test_create_threads_rationale_into_insert():
    db = MagicMock()
    db.execute = AsyncMock(return_value="INSERT 0 1")
    repo = WatcherRunsRepository(db)
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    await repo.create(window_start=now, window_end=now, action="no_issue", rationale="benign noise")
    sql, *args = db.execute.await_args.args
    assert "rationale" in sql
    assert "benign noise" in args


# ---- SH-P2.4: contiguous window + tick diagnostics ---------------------------


@pytest.mark.asyncio
async def test_create_threads_tick_diagnostics_into_insert():
    db = MagicMock()
    db.execute = AsyncMock(return_value="INSERT 0 1")
    repo = WatcherRunsRepository(db)
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    await repo.create(
        window_start=now,
        window_end=now,
        action="error",
        briefing_failures=["open_tickets"],
        retried_job_id="job-a",
        analysis_cost_usd=2.0,
        unanalysed_seconds=900,
        log_lines_truncated=True,
    )
    sql, *args = db.execute.await_args.args
    for column in (
        "briefing_failures",
        "retried_job_id",
        "analysis_cost_usd",
        "unanalysed_seconds",
        "log_lines_truncated",
    ):
        assert column in sql
    assert 900 in args and True in args
    assert ["open_tickets"] in args and "job-a" in args and 2.0 in args


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_last_analysed_window_end_ignores_skipped_and_errored_ticks(db_with_migrations):
    from datetime import UTC, datetime, timedelta

    repo = WatcherRunsRepository(db_with_migrations)
    assert await repo.last_analysed_window_end() is None

    t0 = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    hour = timedelta(hours=1)
    await repo.create(window_start=t0 - hour, window_end=t0, action="no_issue")
    for i, action in enumerate(("skipped_quiet", "skipped_backlog", "error"), start=1):
        await repo.create(window_start=t0, window_end=t0 + i * hour, action=action)
    assert await repo.last_analysed_window_end() == t0

    for i, action in enumerate(("skipped_duplicate", "proposed"), start=4):
        await repo.create(window_start=t0, window_end=t0 + i * hour, action=action)
        assert await repo.last_analysed_window_end() == t0 + i * hour


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_tick_diagnostics_round_trip(db_with_migrations):
    from datetime import UTC, datetime

    repo = WatcherRunsRepository(db_with_migrations)
    now = datetime.now(UTC)
    await repo.create(
        window_start=now,
        window_end=now,
        action="error",
        briefing_failures=["commits", "deploys"],
        retried_job_id="job-a",
        analysis_cost_usd=1.5,
        unanalysed_seconds=4200,
        log_lines_truncated=True,
    )
    await repo.create(window_start=now, window_end=now, action="skipped_quiet")
    quiet, errored = await repo.list_recent(limit=2)
    assert errored["briefing_failures"] == ["commits", "deploys"]
    assert errored["retried_job_id"] == "job-a"
    assert errored["analysis_cost_usd"] == 1.5
    assert errored["unanalysed_seconds"] == 4200 and errored["log_lines_truncated"] is True
    assert quiet["unanalysed_seconds"] == 0 and quiet["log_lines_truncated"] is False
    assert quiet["briefing_failures"] == []
    assert quiet["retried_job_id"] is None and quiet["analysis_cost_usd"] is None


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_unparseable_backoff_reports_only_an_unparseable_last_attempt(db_with_migrations):
    from datetime import UTC, datetime

    from embry0.storage.repositories.watcher_runs import UNPARSEABLE_DETAIL_PREFIX

    repo = WatcherRunsRepository(db_with_migrations)
    now = datetime.now(UTC)

    async def tick(action, **kw):
        await repo.create(window_start=now, window_end=now, action=action, **kw)

    assert await repo.unparseable_backoff() is None

    await tick("error", log_lines=40, detail=f"{UNPARSEABLE_DETAIL_PREFIX} after 2 attempts: empty deliverable")
    assert await repo.unparseable_backoff() == {"log_lines": 40, "skipped_since": 0}

    # Skipped ticks do not reset the state; suppressed ones are counted.
    await tick("skipped_unparseable", log_lines=40)
    await tick("skipped_quiet", log_lines=1)
    await tick("skipped_unparseable", log_lines=40)
    assert await repo.unparseable_backoff() == {"log_lines": 40, "skipped_since": 2}

    # A transient failure is the newest attempt → no back-off, retry at once.
    await tick("error", log_lines=40, detail="analysis job status=failed")
    assert await repo.unparseable_backoff() is None

    await tick("error", log_lines=41, detail=f"{UNPARSEABLE_DETAIL_PREFIX} after 2 attempts: x")
    assert (await repo.unparseable_backoff())["skipped_since"] == 0
    await tick("no_issue", log_lines=41)
    assert await repo.unparseable_backoff() is None


def test_skipped_unparseable_is_a_known_action_that_does_not_advance_the_window():
    from embry0.storage.repositories.watcher_runs import ANALYSED_ACTIONS, WATCHER_ACTIONS

    assert "skipped_unparseable" in WATCHER_ACTIONS
    assert "skipped_unparseable" not in ANALYSED_ACTIONS


def test_a_quiet_tick_is_the_one_verdict_that_is_not_an_analysis():
    # 2026-09-27: the dead-man gauge advances on VERDICT_ACTIONS, the query
    # window on ANALYSED_ACTIONS; the two differ by skipped_quiet and nothing else.
    from embry0.storage.repositories.watcher_runs import ANALYSED_ACTIONS, VERDICT_ACTIONS, WATCHER_ACTIONS

    assert VERDICT_ACTIONS - ANALYSED_ACTIONS == {"skipped_quiet"}
    assert ANALYSED_ACTIONS < VERDICT_ACTIONS < WATCHER_ACTIONS
    for never_a_verdict in ("skipped_backlog", "skipped_analysis_cap", "skipped_unparseable", "error"):
        assert never_a_verdict not in VERDICT_ACTIONS, never_a_verdict
