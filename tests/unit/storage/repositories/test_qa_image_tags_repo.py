import pytest

from embry0.storage.repositories.qa_image_tags import (
    QAImageTagsRepository,
)

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
async def repo(db_with_migrations):
    return QAImageTagsRepository(db=db_with_migrations)


@pytest.mark.asyncio
async def test_record_tag_then_get_active(repo: QAImageTagsRepository):
    await repo.record(
        repo="org/r1",
        image_tag="org_r1:2026-05-05T06-00",
        lockfile_sha="abc" * 8,
        built_by="cli",
    )
    active = await repo.get_active("org/r1")
    assert active is not None
    assert active.image_tag == "org_r1:2026-05-05T06-00"
    assert active.is_active is True


@pytest.mark.asyncio
async def test_record_same_lockfile_returns_existing(repo: QAImageTagsRepository):
    """Composite uniqueness on (repo, lockfile_sha) — re-recording same hash
    must NOT create a duplicate row but should bump built_at."""
    await repo.record(repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli")
    await repo.record(repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli")
    rows = await repo.list_for_repo("org/r1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_get_active_returns_most_recent(repo: QAImageTagsRepository):
    """Multiple lockfile shas → multiple rows. Active = most recent."""
    await repo.record(repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli")
    await repo.record(repo="org/r1", image_tag="t2", lockfile_sha="bbb", built_by="cli")
    active = await repo.get_active("org/r1")
    assert active.image_tag == "t2"


@pytest.mark.asyncio
async def test_deactivate_marks_row_inactive(repo: QAImageTagsRepository):
    await repo.record(repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli")
    await repo.deactivate(repo="org/r1", image_tag="t1")
    active = await repo.get_active("org/r1")
    assert active is None


# --- #334: exactly one active row per repo; base image digest recorded ------


@pytest.mark.asyncio
async def test_record_deactivates_the_repos_previous_rows(repo: QAImageTagsRepository):
    await repo.record(repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli")
    await repo.record(repo="org/r1", image_tag="t2", lockfile_sha="bbb", built_by="cli")
    rows = {r.image_tag: r.is_active for r in await repo.list_for_repo("org/r1")}
    assert rows == {"t1": False, "t2": True}


@pytest.mark.asyncio
async def test_record_leaves_other_repos_alone(repo: QAImageTagsRepository):
    await repo.record(repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli")
    await repo.record(repo="org/r2", image_tag="u1", lockfile_sha="aaa", built_by="cli")
    assert (await repo.get_active("org/r1")).image_tag == "t1"
    assert (await repo.get_active("org/r2")).image_tag == "u1"


@pytest.mark.asyncio
async def test_rerecording_an_old_lockfile_makes_that_build_the_only_active_row(repo: QAImageTagsRepository):
    """A lockfile that reverts is rebuilt under a NEW tag; the upsert reuses the
    old (repo, lockfile_sha) row. That fresh build must be the single active
    row — before #334 both rows stayed active and ordering by built_at picked
    the winner."""
    await repo.record(repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli")
    await repo.record(repo="org/r1", image_tag="t2", lockfile_sha="bbb", built_by="cli")
    await repo.record(repo="org/r1", image_tag="t3", lockfile_sha="aaa", built_by="cli")
    rows = {r.image_tag: r.is_active for r in await repo.list_for_repo("org/r1")}
    assert rows == {"t3": True, "t2": False}


@pytest.mark.asyncio
async def test_two_active_rows_for_one_repo_are_refused_by_the_database(repo: QAImageTagsRepository):
    import asyncpg

    await repo.record(repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli")
    with pytest.raises(asyncpg.UniqueViolationError):
        await repo.db.execute(
            "INSERT INTO qa_image_tags (repo, image_tag, lockfile_sha, is_active) VALUES ('org/r1', 't2', 'bbb', true)"
        )


@pytest.mark.asyncio
async def test_concurrent_records_for_one_repo_leave_one_active_row(repo: QAImageTagsRepository):
    import asyncio

    await asyncio.gather(
        *(repo.record(repo="org/r1", image_tag=f"t{i}", lockfile_sha=f"sha{i}", built_by="cli") for i in range(8))
    )
    rows = await repo.list_for_repo("org/r1")
    assert len(rows) == 8
    assert sum(r.is_active for r in rows) == 1


@pytest.mark.asyncio
async def test_record_round_trips_the_base_image_digest(repo: QAImageTagsRepository):
    await repo.record(
        repo="org/r1",
        image_tag="t1",
        lockfile_sha="aaa",
        built_by="cli",
        base_image="registry:5000/embry0-sandbox-qa:latest",
        base_image_digest="sha256:" + "a" * 64,
    )
    active = await repo.get_active("org/r1")
    assert active.base_image == "registry:5000/embry0-sandbox-qa:latest"
    assert active.base_image_digest == "sha256:" + "a" * 64


@pytest.mark.asyncio
async def test_base_digest_defaults_to_unknown(repo: QAImageTagsRepository):
    await repo.record(repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli")
    active = await repo.get_active("org/r1")
    assert active.base_image is None
    assert active.base_image_digest is None


@pytest.mark.asyncio
async def test_rerecord_replaces_the_base_digest_rather_than_keeping_the_old_one(repo: QAImageTagsRepository):
    await repo.record(
        repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli", base_image="b", base_image_digest="sha256:1"
    )
    await repo.record(
        repo="org/r1", image_tag="t2", lockfile_sha="aaa", built_by="cli", base_image="b", base_image_digest="sha256:2"
    )
    assert (await repo.get_active("org/r1")).base_image_digest == "sha256:2"


@pytest.mark.asyncio
async def test_a_digest_without_its_image_name_is_refused(repo: QAImageTagsRepository):
    with pytest.raises(ValueError, match="base_image"):
        await repo.record(
            repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli", base_image_digest="sha256:1"
        )


@pytest.mark.asyncio
async def test_list_active_returns_one_row_per_repo(repo: QAImageTagsRepository):
    await repo.record(repo="org/r1", image_tag="t1", lockfile_sha="aaa", built_by="cli")
    await repo.record(repo="org/r1", image_tag="t2", lockfile_sha="bbb", built_by="cli")
    await repo.record(repo="org/r2", image_tag="u1", lockfile_sha="aaa", built_by="cli")
    assert [(r.repo, r.image_tag) for r in await repo.list_active()] == [("org/r1", "t2"), ("org/r2", "u1")]
