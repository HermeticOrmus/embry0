"""Repo records — Markdown documents about one registered repository, with history.

A record is addressed by ``(repo, slug)``; its content is a sequence of
revisions numbered from 1, and the record row carries the current one. Writing
the same title and body again adds no revision, which is what makes
``embry0 records import`` idempotent. Design:
docs/superpowers/specs/2026-10-14-repo-records-design.md.

This layer validates what it stores (kind, slug, body size) so every caller —
the API, and later the pipelines — gets the same refusal.
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg
import structlog

from embry0.storage.database import DatabasePool

logger = structlog.get_logger(__name__)

# Restated in migration 95's CHECK constraints; tests pin the two together.
RECORD_KINDS: tuple[str, ...] = ("rca", "audit", "spec", "plan", "spike", "runbook", "note")
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
# 512 KiB of UTF-8. Not 1 MiB: the dashboard's nginx keeps its default
# client_max_body_size of 1 MiB for every /api/ route, and a JSON-encoded body
# needs headroom for its escapes, so a larger cap would be refused by nginx
# with an HTML 413 before this clear one could answer.
MAX_BODY_BYTES = 512 * 1024
MAX_TITLE_CHARS = 300
MAX_LIST_LIMIT = 1000

_HEADING_RE = re.compile(r"^#[ \t]+(.+?)[ \t#]*$", re.MULTILINE)

_SUMMARY_SQL = """
    SELECT r.slug, r.kind, r.title, r.current_revision, r.created_at, r.updated_at,
           v.author AS updated_by, octet_length(v.body) AS size_bytes
      FROM repo_records r
      JOIN repo_record_revisions v ON v.record_id = r.id AND v.revision = r.current_revision
"""


class RecordError(ValueError):
    """A record write the store refuses; ``status`` is the HTTP answer for it."""

    status = 422


class RecordTooLargeError(RecordError):
    status = 413


class RecordKindConflictError(RecordError):
    status = 409


class RepoNotRegisteredError(RecordError):
    status = 404


class RepoHasRecordsError(RecordError):
    status = 409


def title_from_body(body: str, fallback: str) -> str:
    """The first ``# `` heading of ``body``, else ``fallback``."""
    match = _HEADING_RE.search(body)
    title = match.group(1).strip() if match else ""
    return (title or fallback)[:MAX_TITLE_CHARS]


def validate_slug(slug: str) -> str:
    if not SLUG_PATTERN.match(slug):
        raise RecordError(
            f"invalid slug {slug!r}: lowercase letters, digits, '.', '_' and '-', "
            "starting with a letter or digit, at most 128 characters"
        )
    return slug


def validate_kind(kind: str) -> str:
    if kind not in RECORD_KINDS:
        raise RecordError(f"invalid kind {kind!r}: expected one of {list(RECORD_KINDS)}")
    return kind


def validate_body(body: str) -> str:
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise RecordTooLargeError(f"record body is {size} bytes; the limit is {MAX_BODY_BYTES} bytes (512 KiB)")
    if not body.strip():
        raise RecordError("record body is empty")
    return body


class RepoRecordsRepository:
    """CRUD for ``repo_records`` and ``repo_record_revisions``."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    async def list_records(
        self, repo: str, *, kind: str | None = None, query: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Current state of each record of ``repo``, most recently updated first.

        ``query`` is a case-insensitive substring of the title or the current
        body. Raises RepoNotRegisteredError for an unregistered repository, so
        a typo is not mistaken for an empty store.
        """
        await self._require_repo(repo)
        if kind is not None:
            validate_kind(kind)
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        rows = await self._db.fetch(
            _SUMMARY_SQL
            + """
             WHERE r.repo = $1
               AND ($2::text IS NULL OR r.kind = $2)
               AND ($3::text IS NULL
                    OR strpos(lower(r.title), lower($3)) > 0
                    OR strpos(lower(v.body), lower($3)) > 0)
             ORDER BY r.updated_at DESC, r.slug
             LIMIT $4
            """,
            repo,
            kind,
            query or None,
            limit,
        )
        return [dict(r) for r in rows]

    async def get(self, repo: str, slug: str, *, revision: int | None = None) -> dict[str, Any] | None:
        """One revision of a record (the current one by default), with its body; None when absent."""
        row = await self._db.fetchrow(
            """
            SELECT r.repo, r.slug, r.kind, r.title AS record_title, r.current_revision,
                   r.created_at, r.updated_at,
                   v.revision, v.title, v.body, v.author, v.source_job_id,
                   v.created_at AS revision_created_at
              FROM repo_records r
              JOIN repo_record_revisions v
                ON v.record_id = r.id AND v.revision = COALESCE($3::int, r.current_revision)
             WHERE r.repo = $1 AND r.slug = $2
            """,
            repo,
            slug,
            revision,
        )
        if row is None:
            return None
        out = dict(row)
        out.pop("record_title")
        return out

    async def revisions(self, repo: str, slug: str) -> list[dict[str, Any]] | None:
        """Metadata of every revision, newest first; None when the record is absent."""
        record_id = await self._db.fetchval("SELECT id FROM repo_records WHERE repo = $1 AND slug = $2", repo, slug)
        if record_id is None:
            return None
        rows = await self._db.fetch(
            """
            SELECT revision, title, author, source_job_id, created_at, octet_length(body) AS size_bytes
              FROM repo_record_revisions WHERE record_id = $1 ORDER BY revision DESC
            """,
            record_id,
        )
        return [dict(r) for r in rows]

    async def put(
        self,
        repo: str,
        slug: str,
        *,
        kind: str,
        body: str,
        title: str | None = None,
        author: str = "",
        source_job_id: str | None = None,
    ) -> dict[str, Any]:
        """Create the record, or append a revision when the title or body changed.

        Returns the record's current state plus ``created`` and ``changed``.
        Raises RecordKindConflictError when the record exists with another
        kind, RepoNotRegisteredError for an unregistered repository, and
        RecordError / RecordTooLargeError for invalid input.
        """
        validate_slug(slug)
        validate_kind(kind)
        validate_body(body)
        title = (title or "").strip()[:MAX_TITLE_CHARS] or title_from_body(body, slug)
        source_job_id = source_job_id or None

        created = changed = False
        async with self._db.transaction() as conn:
            if not await conn.fetchval("SELECT 1 FROM repos WHERE repo = $1", repo):
                raise RepoNotRegisteredError(f"repo {repo!r} is not registered; register it first (POST /repos)")
            # The row lock serialises writers of one record, so two of them
            # cannot both compute revision n + 1.
            existing = await conn.fetchrow(
                "SELECT id, kind, current_revision FROM repo_records WHERE repo = $1 AND slug = $2 FOR UPDATE",
                repo,
                slug,
            )
            if existing is None:
                record_id = await self._insert_record(conn, repo, slug, kind, title)
                if record_id is None:
                    # A concurrent writer created it between our read and insert.
                    existing = await conn.fetchrow(
                        "SELECT id, kind, current_revision FROM repo_records WHERE repo = $1 AND slug = $2 FOR UPDATE",
                        repo,
                        slug,
                    )
                else:
                    await self._insert_revision(conn, record_id, 1, title, body, author, source_job_id)
                    created, changed = True, True
            if existing is not None:
                if existing["kind"] != kind:
                    raise RecordKindConflictError(
                        f"record {slug!r} of {repo!r} is a {existing['kind']!r}, not a {kind!r}; "
                        "the kind of a record is fixed when it is created"
                    )
                created = False
                latest = await conn.fetchrow(
                    "SELECT title, body FROM repo_record_revisions WHERE record_id = $1 AND revision = $2",
                    existing["id"],
                    existing["current_revision"],
                )
                changed = latest is None or latest["title"] != title or latest["body"] != body
                if changed:
                    revision = int(existing["current_revision"]) + 1
                    await self._insert_revision(conn, existing["id"], revision, title, body, author, source_job_id)
                    await conn.execute(
                        "UPDATE repo_records SET current_revision = $2, title = $3, updated_at = NOW() WHERE id = $1",
                        existing["id"],
                        revision,
                        title,
                    )
        current = await self.get(repo, slug)
        assert current is not None
        if changed:
            logger.info(
                "repo_record_written",
                repo=repo,
                slug=slug,
                kind=kind,
                revision=current["revision"],
                created=created,
                author=author,
            )
        return {**current, "created": created, "changed": changed}

    async def delete(self, repo: str, slug: str) -> bool:
        """Delete a record and every revision. False when it did not exist."""
        result = await self._db.execute("DELETE FROM repo_records WHERE repo = $1 AND slug = $2", repo, slug)
        return str(result).endswith(" 1")

    async def count_for_repo(self, repo: str) -> int:
        return int(await self._db.fetchval("SELECT count(*) FROM repo_records WHERE repo = $1", repo) or 0)

    async def _require_repo(self, repo: str) -> None:
        if not await self._db.fetchval("SELECT 1 FROM repos WHERE repo = $1", repo):
            raise RepoNotRegisteredError(f"repo {repo!r} is not registered")

    @staticmethod
    async def _insert_record(conn: asyncpg.Connection, repo: str, slug: str, kind: str, title: str) -> int | None:
        record_id = await conn.fetchval(
            """
            INSERT INTO repo_records (repo, slug, kind, title, current_revision)
            VALUES ($1, $2, $3, $4, 1)
            ON CONFLICT (repo, slug) DO NOTHING
            RETURNING id
            """,
            repo,
            slug,
            kind,
            title,
        )
        return int(record_id) if record_id is not None else None

    @staticmethod
    async def _insert_revision(
        conn: asyncpg.Connection,
        record_id: int,
        revision: int,
        title: str,
        body: str,
        author: str,
        source_job_id: str | None,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO repo_record_revisions (record_id, revision, title, body, author, source_job_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            record_id,
            revision,
            title,
            body,
            author,
            source_job_id,
        )
