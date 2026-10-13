"""RAV-1063: connection-scoped CAS transition helpers + boot reconciliation.

Pure-plumbing tests (house pattern: mock the executor/pool, assert bound
args) — the real-Postgres transactional behaviour is covered by the
end-to-end test in test_database.py.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.storage.repositories.issues import IssuesRepository
from embry0.storage.repositories.jobs import JobsRepository

pytestmark = pytest.mark.asyncio


def _jobs_repo():
    return JobsRepository(MagicMock())


def _issues_repo():
    return IssuesRepository(MagicMock())


# ---- cas_transition_on_conn (jobs) ------------------------------------------


async def test_jobs_cas_matched_returns_true_with_bound_args():
    conn = MagicMock(fetchrow=AsyncMock(return_value={"job_id": "job-1"}))
    ok = await _jobs_repo().cas_transition_on_conn(conn, "job-1", "awaiting_input", {"running"})
    assert ok is True
    query, job_id, new_status, expected = conn.fetchrow.await_args.args
    assert "UPDATE jobs SET status = $2" in query
    assert "OR status = $2" in query, "already-in-target must match (idempotent re-entry)"
    assert job_id == "job-1"
    assert new_status == "awaiting_input"
    assert expected == ["running"]


async def test_jobs_cas_unmatched_returns_false():
    conn = MagicMock(fetchrow=AsyncMock(return_value=None))
    ok = await _jobs_repo().cas_transition_on_conn(conn, "job-1", "awaiting_input", {"running"})
    assert ok is False


async def test_jobs_cas_illegal_transition_raises_before_sql():
    conn = MagicMock(fetchrow=AsyncMock())
    with pytest.raises(ValueError, match="Invalid job status transition"):
        # pending -> awaiting_input is not in VALID_JOB_TRANSITIONS.
        await _jobs_repo().cas_transition_on_conn(conn, "job-1", "awaiting_input", {"pending"})
    conn.fetchrow.assert_not_awaited()


async def test_jobs_cas_validates_every_expected_state():
    conn = MagicMock(fetchrow=AsyncMock())
    with pytest.raises(ValueError):
        # running -> awaiting_input is fine, but paused -> awaiting_input is not.
        await _jobs_repo().cas_transition_on_conn(conn, "job-1", "awaiting_input", {"running", "paused"})
    conn.fetchrow.assert_not_awaited()


# ---- cas_transition_on_conn (issues) ----------------------------------------


async def test_issues_cas_matched_returns_true_with_bound_args():
    conn = MagicMock(fetchrow=AsyncMock(return_value={"id": "iss-1"}))
    ok = await _issues_repo().cas_transition_on_conn(conn, "iss-1", "awaiting_input", {"triaging", "in_progress"})
    assert ok is True
    query, issue_id, new_status, expected = conn.fetchrow.await_args.args
    assert "UPDATE issues SET status = $2" in query
    assert "updated_at = NOW()" in query
    assert issue_id == "iss-1"
    assert new_status == "awaiting_input"
    assert expected == ["in_progress", "triaging"]  # sorted


async def test_issues_cas_unmatched_returns_false():
    conn = MagicMock(fetchrow=AsyncMock(return_value=None))
    ok = await _issues_repo().cas_transition_on_conn(conn, "iss-1", "open", {"paused"})
    assert ok is False


async def test_issues_cas_illegal_transition_raises_before_sql():
    conn = MagicMock(fetchrow=AsyncMock())
    with pytest.raises(ValueError, match="Invalid issue status transition"):
        # closed -> awaiting_input is not a valid transition.
        await _issues_repo().cas_transition_on_conn(conn, "iss-1", "awaiting_input", {"closed"})
    conn.fetchrow.assert_not_awaited()


# ---- boot reconciliation ----------------------------------------------------


async def test_fail_orphaned_binds_message_and_code_and_returns_ids():
    db = MagicMock(fetch=AsyncMock(return_value=[{"job_id": "job-a"}, {"job_id": "job-b"}]))
    repo = JobsRepository(db)
    ids = await repo.fail_orphaned("ERR_ORPHANED", "Orchestrator restarted", never_started_message="Never started")
    assert ids == ["job-a", "job-b"]
    query, message, code, never_started, keep, interrupted_code, _ = db.fetch.await_args.args
    assert "WHERE status IN ('running', 'pending')" in query
    assert "RETURNING job_id" in query
    assert message == "Orchestrator restarted"
    assert code == "ERR_ORPHANED"
    assert never_started == "Never started"
    assert keep == [] and interrupted_code is None, "with no keep / interrupted code the sweep fails every orphan alike"


async def test_reopen_stuck_for_jobs_binds_ids_and_counts():
    db = MagicMock(fetch=AsyncMock(return_value=[{"id": "iss-1"}]))
    repo = IssuesRepository(db)
    n = await repo.reopen_stuck_for_jobs(["job-a", "job-b"])
    assert n == 1
    query, job_ids = db.fetch.await_args.args
    assert "status IN ('triaging', 'awaiting_input')" in query
    assert "issue_id IS NOT NULL" in query
    assert job_ids == ["job-a", "job-b"]
