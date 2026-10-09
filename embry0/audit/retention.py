"""Audit retention sweeper (RAV-1056 Phase C).

Both audit sinks grow unbounded without this: the ``audit_log`` table and
the JSONL file. A daily loop in the app lifespan calls
``sweep_audit_retention``; each half fails independently and silently
(mirroring the write path's swallow-errors policy — retention must never
take the orchestrator down).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from embry0.audit import logger as audit_logger

if TYPE_CHECKING:
    from embry0.storage.repositories.audit import AuditRepository

logger = structlog.get_logger(__name__)


def _line_expired(line: str, cutoff: datetime) -> bool:
    """True only when the line parses AND is older than cutoff.

    Malformed or undatable lines are conservatively kept — never delete
    data we can't date.
    """
    try:
        ts = datetime.fromisoformat(json.loads(line)["timestamp"])
    except (ValueError, KeyError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts < cutoff


def prune_jsonl_file(path: Path, cutoff: datetime) -> int:
    """Drop lines older than cutoff; returns the number pruned.

    Sync — call via ``asyncio.to_thread``. Streaming filter into a temp file
    in the same directory, then under the appender's ``_FILE_LOCK`` drain any
    bytes appended during the scan (via the recorded offset) and atomically
    ``os.replace``. The lock is held only for the tail-drain + rename, not
    the full scan.
    """
    if not path.exists():
        return 0
    tmp = path.with_name(path.name + ".sweep.tmp")
    pruned = 0
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            for raw in src:
                if _line_expired(raw.decode("utf-8", errors="replace"), cutoff):
                    pruned += 1
                else:
                    dst.write(raw)
            offset = src.tell()
            if pruned == 0:
                return 0
            with audit_logger._FILE_LOCK:
                with open(path, "rb") as tail_src:
                    tail_src.seek(offset)
                    shutil.copyfileobj(tail_src, dst)
                dst.flush()
                os.replace(tmp, path)
        return pruned
    finally:
        tmp.unlink(missing_ok=True)


async def sweep_audit_retention(
    audit_repo: AuditRepository | None,
    audit_log_path: Path | None,
    retention_days: int,
) -> tuple[int, int]:
    """Prune both sinks; returns (db_rows_deleted, jsonl_lines_pruned)."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    db_deleted = 0
    if audit_repo is not None:
        try:
            db_deleted = await audit_repo.delete_older_than(cutoff)
        except Exception:
            logger.warning("audit_retention_db_sweep_failed", exc_info=True)

    file_pruned = 0
    if audit_log_path:
        try:
            file_pruned = await asyncio.to_thread(prune_jsonl_file, audit_log_path, cutoff)
        except Exception:
            logger.warning("audit_retention_file_sweep_failed", exc_info=True, path=str(audit_log_path))

    return db_deleted, file_pruned
