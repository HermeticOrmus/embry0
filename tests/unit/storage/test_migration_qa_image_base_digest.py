"""Migration: qa_image_tags base-image digest + one active row per repo (#334).

VERSION is a constant because wave-2 migration numbers are reassigned by the
coordinator at merge time — renumbering is this one line plus the runner entry.
"""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS

VERSION = 75


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == VERSION]
    assert len(match) == 1
    return match[0]


def test_migration_is_present_exactly_once() -> None:
    """Was "is the last entry" until #289 added 76. The "last entry" assertion
    now lives in the newest migration's guard
    (``test_migration_log_signatures``), which also carries version-agnostic
    uniqueness and contiguity checks. The intent here — this version exists once
    and nothing duplicated it — is kept, not weakened."""
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions.count(VERSION) == 1
    assert max(versions) >= VERSION


def test_migration_never_deletes_rows_or_touches_image_tags() -> None:
    sql = _sql()
    assert "DELETE" not in sql.upper()
    assert "DROP TABLE" not in sql.upper()
    assert "SET is_active = false" in sql
    assert sql.upper().count("UPDATE QA_IMAGE_TAGS") == 1


async def _drop_new_objects(db: DatabasePool) -> None:
    """Rewind the table to its pre-migration shape so legacy data can be seeded."""
    await db.execute("DROP INDEX IF EXISTS uq_qa_image_tags_one_active_per_repo")
    await db.execute("ALTER TABLE qa_image_tags DROP CONSTRAINT IF EXISTS ck_qa_image_tags_base_pair")
    await db.execute("ALTER TABLE qa_image_tags DROP COLUMN IF EXISTS base_image")
    await db.execute("ALTER TABLE qa_image_tags DROP COLUMN IF EXISTS base_image_digest")


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_keeps_only_the_newest_active_row_per_repo(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    await _drop_new_objects(db)
    # The production shape on 2026-09-20: two active rows for one repo.
    await db.execute(
        """
        INSERT INTO qa_image_tags (repo, image_tag, lockfile_sha, built_at, is_active, metadata) VALUES
          ('acme/console', 'cc:2026-08-12', 'f649', '2026-08-12T22:33:09Z', true,  '{"branch":"master"}'),
          ('acme/console', 'cc:2026-09-20', '80f4', '2026-09-20T05:00:40Z', true,  '{"branch":"master"}'),
          ('org/solo',                   'solo:1',        'aaaa', '2026-01-01T00:00:00Z', true,  '{}'),
          ('org/retired',                'ret:1',         'aaaa', '2026-01-01T00:00:00Z', false, '{}'),
          ('org/tie',                    'tie:a',         'aaaa', '2026-01-01T00:00:00Z', true,  '{}'),
          ('org/tie',                    'tie:b',         'bbbb', '2026-01-01T00:00:00Z', true,  '{}')
        """
    )
    before = await db.fetch("SELECT image_tag, built_at, metadata FROM qa_image_tags ORDER BY image_tag")

    await db.execute(_sql())
    await db.execute(_sql())  # idempotent

    active = {
        r["repo"]: r["image_tag"]
        for r in await db.fetch("SELECT repo, image_tag FROM qa_image_tags WHERE is_active ORDER BY repo")
    }
    assert active["acme/console"] == "cc:2026-09-20"
    assert active["org/solo"] == "solo:1"
    assert "org/retired" not in active
    assert active["org/tie"] in {"tie:a", "tie:b"}  # exactly one survives an exact tie
    assert len(active) == 3

    after = await db.fetch("SELECT image_tag, built_at, metadata FROM qa_image_tags ORDER BY image_tag")
    assert [tuple(r) for r in after] == [tuple(r) for r in before]  # nothing but is_active moved
    assert await db.fetchval("SELECT COUNT(*) FROM qa_image_tags WHERE base_image_digest IS NOT NULL") == 0


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_migration_pairs_base_image_with_its_digest(db_with_migrations: DatabasePool) -> None:
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        await db_with_migrations.execute(
            "INSERT INTO qa_image_tags (repo, image_tag, lockfile_sha, base_image_digest) "
            "VALUES ('org/r', 't', 'a', 'sha256:1')"
        )
