"""RepoRecordsRepository against Postgres: revisions, idempotency, refusals."""

from __future__ import annotations

import asyncio

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.repositories.repo_records import (
    MAX_BODY_BYTES,
    RecordError,
    RecordKindConflictError,
    RecordTooLargeError,
    RepoNotRegisteredError,
    RepoRecordsRepository,
    title_from_body,
)

pytestmark = pytest.mark.requires_postgres

REPO = "acme/widgets"
NESTED = "group/sub/project"


@pytest.fixture
async def records(db_with_migrations: DatabasePool) -> RepoRecordsRepository:
    for repo in (REPO, NESTED, "acme/other"):
        await db_with_migrations.execute("INSERT INTO repos (repo, forge) VALUES ($1, 'github')", repo)
    return RepoRecordsRepository(db_with_migrations)


@pytest.mark.asyncio
async def test_create_then_read(records: RepoRecordsRepository) -> None:
    out = await records.put(REPO, "outage", kind="rca", body="# Outage\n\nIt broke.", author="key:ops")
    assert out["created"] is True and out["changed"] is True
    assert out["revision"] == 1 and out["current_revision"] == 1
    assert out["title"] == "Outage", "the title must default to the first heading"
    got = await records.get(REPO, "outage")
    assert got is not None
    assert got["body"] == "# Outage\n\nIt broke."
    assert got["author"] == "key:ops"
    assert got["kind"] == "rca"


@pytest.mark.asyncio
async def test_identical_put_adds_no_revision(records: RepoRecordsRepository) -> None:
    await records.put(REPO, "outage", kind="rca", body="# Outage\n")
    again = await records.put(REPO, "outage", kind="rca", body="# Outage\n")
    assert again["created"] is False
    assert again["changed"] is False, "an identical body and title must not add a revision"
    assert again["current_revision"] == 1
    revisions = await records.revisions(REPO, "outage")
    assert revisions is not None and [r["revision"] for r in revisions] == [1]


@pytest.mark.asyncio
async def test_changed_body_appends_and_old_revision_stays_readable(records: RepoRecordsRepository) -> None:
    await records.put(REPO, "outage", kind="rca", body="# Outage\n\nfirst")
    second = await records.put(REPO, "outage", kind="rca", body="# Outage v2\n\nsecond", author="alice")
    assert second["changed"] is True and second["current_revision"] == 2
    assert second["title"] == "Outage v2"
    first = await records.get(REPO, "outage", revision=1)
    assert first is not None and first["body"] == "# Outage\n\nfirst" and first["title"] == "Outage"
    assert first["current_revision"] == 2
    revisions = await records.revisions(REPO, "outage")
    assert revisions is not None
    assert [(r["revision"], r["title"], r["author"]) for r in revisions] == [
        (2, "Outage v2", "alice"),
        (1, "Outage", ""),
    ]
    assert await records.get(REPO, "outage", revision=3) is None


@pytest.mark.asyncio
async def test_title_only_change_is_a_new_revision(records: RepoRecordsRepository) -> None:
    await records.put(REPO, "plan", kind="plan", body="body", title="One")
    out = await records.put(REPO, "plan", kind="plan", body="body", title="Two")
    assert out["changed"] is True and out["current_revision"] == 2 and out["title"] == "Two"


@pytest.mark.asyncio
async def test_kind_is_fixed_at_creation(records: RepoRecordsRepository) -> None:
    await records.put(REPO, "outage", kind="rca", body="x")
    with pytest.raises(RecordKindConflictError):
        await records.put(REPO, "outage", kind="audit", body="x")


@pytest.mark.asyncio
async def test_unregistered_repo_is_refused(records: RepoRecordsRepository) -> None:
    with pytest.raises(RepoNotRegisteredError):
        await records.put("acme/ghost", "a", kind="note", body="x")
    with pytest.raises(RepoNotRegisteredError):
        await records.list_records("acme/ghost")


@pytest.mark.asyncio
async def test_body_cap_and_input_validation(records: RepoRecordsRepository) -> None:
    await records.put(REPO, "big", kind="note", body="x" * MAX_BODY_BYTES)  # exactly the cap is fine
    with pytest.raises(RecordTooLargeError, match="512 KiB"):
        await records.put(REPO, "bigger", kind="note", body="x" * (MAX_BODY_BYTES + 1))
    with pytest.raises(RecordTooLargeError):
        # The cap is in bytes, not characters.
        await records.put(REPO, "wide", kind="note", body="é" * (MAX_BODY_BYTES // 2 + 1))
    with pytest.raises(RecordError):
        await records.put(REPO, "Bad Slug", kind="note", body="x")
    with pytest.raises(RecordError):
        await records.put(REPO, "ok", kind="diary", body="x")
    with pytest.raises(RecordError):
        await records.put(REPO, "ok", kind="note", body="   \n")


@pytest.mark.asyncio
async def test_list_filters_and_scopes_by_repo(records: RepoRecordsRepository) -> None:
    await records.put(REPO, "outage", kind="rca", body="# Outage\n\nThe cache fell over.")
    await records.put(REPO, "runbook", kind="runbook", body="# Restart\n\nRestart the CACHE.")
    await records.put(REPO, "notes", kind="note", body="# Misc\n\nnothing")
    await records.put(NESTED, "outage", kind="rca", body="# Elsewhere\n\ncache")
    all_rows = await records.list_records(REPO)
    assert {r["slug"] for r in all_rows} == {"outage", "runbook", "notes"}, "another repo's records leaked"
    assert [r["slug"] for r in all_rows][0] == "notes", "newest first"
    assert {r["slug"] for r in await records.list_records(REPO, kind="rca")} == {"outage"}
    assert {r["slug"] for r in await records.list_records(REPO, query="cache")} == {"outage", "runbook"}
    assert {r["slug"] for r in await records.list_records(REPO, query="MISC")} == {"notes"}
    assert {r["slug"] for r in await records.list_records(REPO, query="50%_")} == set(), "no LIKE wildcards"
    row = next(r for r in all_rows if r["slug"] == "outage")
    assert row["size_bytes"] == len(b"# Outage\n\nThe cache fell over.")
    assert {r["slug"] for r in await records.list_records(NESTED)} == {"outage"}


@pytest.mark.asyncio
async def test_delete_removes_history(records: RepoRecordsRepository) -> None:
    await records.put(REPO, "a", kind="note", body="1")
    await records.put(REPO, "a", kind="note", body="2")
    assert await records.delete(REPO, "a") is True
    assert await records.get(REPO, "a") is None
    assert await records.revisions(REPO, "a") is None
    assert await records.delete(REPO, "a") is False


@pytest.mark.asyncio
async def test_concurrent_writers_get_distinct_revisions(records: RepoRecordsRepository) -> None:
    await records.put(REPO, "busy", kind="note", body="0")
    results = await asyncio.gather(*(records.put(REPO, "busy", kind="note", body=str(i)) for i in range(1, 5)))
    assert all(r["changed"] for r in results)
    revisions = await records.revisions(REPO, "busy")
    assert revisions is not None and sorted(r["revision"] for r in revisions) == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_concurrent_creates_of_one_slug(records: RepoRecordsRepository) -> None:
    results = await asyncio.gather(*(records.put(REPO, "new", kind="note", body=f"b{i}") for i in range(4)))
    assert sum(1 for r in results if r["created"]) == 1, "exactly one writer creates the record"
    revisions = await records.revisions(REPO, "new")
    assert revisions is not None and sorted(r["revision"] for r in revisions) == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_source_job_id_is_kept_per_revision(records: RepoRecordsRepository) -> None:
    await records.put(REPO, "a", kind="note", body="1", source_job_id="job-1")
    await records.put(REPO, "a", kind="note", body="2")
    assert (await records.get(REPO, "a", revision=1) or {})["source_job_id"] == "job-1"
    assert (await records.get(REPO, "a") or {})["source_job_id"] is None


def test_title_from_body() -> None:
    assert title_from_body("intro\n# Real title ##\n# second", "slug") == "Real title"
    assert title_from_body("## only a subheading", "slug") == "slug"
    assert title_from_body("#nospace", "slug") == "slug"
