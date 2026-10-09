"""DB-backed per-repo QA config — the qa.yaml v2 document in Postgres (RAV-1251).

Replaces the Phase-1 file store (``repo-configs/<owner>__<repo>/qa.yaml``) as
the source of truth: one row per repo holding the raw yaml text. Callers
validate the text with ``parse_qa_yaml_v2`` BEFORE upserting — this layer
stores what it is given. Upserts auto-register the repo in the ``repos``
registry so a configured repo always appears in listings.
"""

from __future__ import annotations

from typing import Any

import structlog

from embry0.storage.database import DatabasePool
from embry0.storage.repositories.repos import DEFAULT_FORGE

logger = structlog.get_logger(__name__)

_COLUMNS = "repo, yaml_text, updated_by, created_at, updated_at"


class RepoQAConfigsRepository:
    """CRUD for the ``repo_qa_configs`` table."""

    def __init__(self, db: DatabasePool, *, default_forge: str = DEFAULT_FORGE) -> None:
        self._db = db
        self._default_forge = default_forge

    async def get(self, repo: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(f"SELECT {_COLUMNS} FROM repo_qa_configs WHERE repo = $1", repo)
        return dict(row) if row else None

    async def upsert(self, repo: str, yaml_text: str, updated_by: str = "") -> dict[str, Any]:
        """Store the config, auto-registering the repo (the FK requires a row).

        The forge is written EXPLICITLY, never left to the column's
        ``DEFAULT 'github'``: that default is a correct backfill for rows that
        predate #162, but as a live insert it is the same silent guess §3.8
        removed from ``repo_register`` — and a guessed row is worse than none,
        because ``forge_for`` then succeeds and ERR_REPO_UNREGISTERED never
        fires again for that repo. A deployment with no configured default
        refuses instead: register the repo with its forge first.
        """
        if not self._default_forge:
            registered = await self._db.fetchrow("SELECT 1 FROM repos WHERE repo = $1", repo)
            if not registered:
                raise ValueError(
                    f"repo {repo!r} is not registered and DEFAULT_FORGE is empty — "
                    "register it with its forge first (POST /repos with "
                    '{"repo": "<path>", "forge": "gitlab"} or the MCP repo_register tool), '
                    "then store its QA config."
                )
        else:
            await self._db.execute(
                "INSERT INTO repos (repo, forge) VALUES ($1, $2) ON CONFLICT (repo) DO NOTHING",
                repo,
                self._default_forge,
            )
        await self._db.execute(
            """
            INSERT INTO repo_qa_configs (repo, yaml_text, updated_by)
            VALUES ($1, $2, $3)
            ON CONFLICT (repo) DO UPDATE SET
                yaml_text = EXCLUDED.yaml_text,
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
            """,
            repo,
            yaml_text,
            updated_by,
        )
        out = await self.get(repo)
        assert out is not None
        return out

    async def delete(self, repo: str) -> None:
        await self._db.execute("DELETE FROM repo_qa_configs WHERE repo = $1", repo)

    async def list_repos(self) -> list[str]:
        rows = await self._db.fetch("SELECT repo FROM repo_qa_configs ORDER BY repo")
        return [r["repo"] for r in rows]
