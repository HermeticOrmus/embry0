"""Audit retention sweeper tests (RAV-1056 Phase C)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.audit.retention import prune_jsonl_file, sweep_audit_retention

CUTOFF = datetime(2026, 6, 1, tzinfo=UTC)


def _line(ts: datetime, action: str = "user.login") -> str:
    return json.dumps({"timestamp": ts.isoformat(), "action": action, "actor": "a", "details": {}}) + "\n"


OLD = _line(CUTOFF - timedelta(days=2))
NEW = _line(CUTOFF + timedelta(days=2))


def test_prune_drops_old_keeps_new(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(OLD + NEW + OLD)
    assert prune_jsonl_file(path, CUTOFF) == 2
    assert path.read_text() == NEW


def test_prune_keeps_malformed_and_undatable_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    garbage = "not json at all\n"
    no_ts = json.dumps({"action": "x"}) + "\n"
    path.write_text(OLD + garbage + no_ts)
    assert prune_jsonl_file(path, CUTOFF) == 1
    assert path.read_text() == garbage + no_ts


def test_prune_missing_file_returns_zero(tmp_path):
    assert prune_jsonl_file(tmp_path / "absent.jsonl", CUTOFF) == 0


def test_prune_nothing_expired_leaves_file_untouched(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(NEW + NEW)
    before = path.stat().st_mtime_ns
    assert prune_jsonl_file(path, CUTOFF) == 0
    assert path.read_text() == NEW + NEW
    assert path.stat().st_mtime_ns == before  # no rewrite happened
    assert not (tmp_path / "audit.jsonl.sweep.tmp").exists()


def test_prune_naive_timestamps_treated_as_utc(tmp_path):
    naive_old = json.dumps({"timestamp": "2026-05-01T00:00:00", "action": "x"}) + "\n"
    path = tmp_path / "audit.jsonl"
    path.write_text(naive_old + NEW)
    assert prune_jsonl_file(path, CUTOFF) == 1
    assert path.read_text() == NEW


def test_prune_drains_concurrent_append(tmp_path, monkeypatch):
    """A line appended between the scan and the lock must survive the swap."""
    import embry0.audit.retention as retention_mod

    path = tmp_path / "audit.jsonl"
    path.write_text(OLD + NEW)
    appended = _line(CUTOFF + timedelta(days=3), action="concurrent")

    class AppendingLock:
        def __enter__(self):
            with open(path, "a") as f:
                f.write(appended)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(retention_mod.audit_logger, "_FILE_LOCK", AppendingLock())
    assert prune_jsonl_file(path, CUTOFF) == 1
    assert path.read_text() == NEW + appended


@pytest.mark.asyncio
async def test_sweep_halves_run_and_report(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(OLD + NEW)
    repo = MagicMock()
    repo.delete_older_than = AsyncMock(return_value=3)
    db_n, file_n = await sweep_audit_retention(repo, path, retention_days=30)
    assert db_n == 3
    assert file_n >= 1  # OLD is far older than any 30-day cutoff
    cutoff_arg = repo.delete_older_than.call_args.args[0]
    assert cutoff_arg.tzinfo is not None


@pytest.mark.asyncio
async def test_sweep_none_halves_return_zero():
    assert await sweep_audit_retention(None, None, retention_days=30) == (0, 0)


@pytest.mark.asyncio
async def test_sweep_db_failure_does_not_break_file_half(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(OLD)
    repo = MagicMock()
    repo.delete_older_than = AsyncMock(side_effect=RuntimeError("db down"))
    db_n, file_n = await sweep_audit_retention(repo, path, retention_days=30)
    assert db_n == 0
    assert file_n == 1
