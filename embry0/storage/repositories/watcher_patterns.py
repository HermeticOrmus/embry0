"""Known-benign pattern registry for the watcher (RAV-1138).

Patterns the analysis agent has judged not ticket-worthy, keyed by
fingerprint. Deliberately NOT age-windowed like watcher_runs: benign noise
recurs indefinitely, and letting it age out of memory means it re-alerts
(and re-costs an analysis verdict) every window. Entries are proposed by
the agent via the ``MEMORY:`` deliverable line and curated here — upsert
by fingerprint, occurrence-counted, list capped at read time.
"""

from __future__ import annotations

from typing import Any

import structlog

from embry0.storage.database import DatabasePool

logger = structlog.get_logger(__name__)


class WatcherPatternsRepository:
    """Upsert/read operations for the watcher_known_patterns table."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    async def upsert(self, fingerprint: str, summary: str, repo: str | None = None) -> None:
        """Record (or re-confirm) a known-benign pattern for one stack.

        Keyed on (fingerprint, repo) since #378: the same benign slug means
        different things on different stacks, and a fingerprint-only key let
        the second stack overwrite the first's note and read it back.
        """
        await self._db.execute(
            """
            INSERT INTO watcher_known_patterns (fingerprint, repo, summary)
            VALUES ($1, $2, $3)
            ON CONFLICT (fingerprint, repo) DO UPDATE SET
                summary = EXCLUDED.summary,
                last_seen = NOW(),
                occurrences = watcher_known_patterns.occurrences + 1
            """,
            fingerprint,
            repo or "",
            summary,
        )
        logger.info("watcher_pattern_upserted", fingerprint=fingerprint, repo=repo or "")

    async def list_recent(self, limit: int = 30, repo: str | None = None) -> list[dict[str, Any]]:
        """Known-benign patterns, most recently re-confirmed first. ``repo``
        narrows to one stack's memory; None reads every stack's."""
        rows = await self._db.fetch(
            """
            SELECT fingerprint, repo, summary, first_seen, last_seen, occurrences
            FROM watcher_known_patterns
            WHERE ($2::text IS NULL OR repo = $2)
            ORDER BY last_seen DESC LIMIT $1
            """,
            limit,
            repo,
        )
        return [dict(r) for r in rows]
