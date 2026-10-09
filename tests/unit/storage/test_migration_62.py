"""Migration 62 — issue_inputs render-time GitHub comment mapping (#253)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS
from embry0.storage.repositories.issue_inputs import IssueInputsRepository
from embry0.storage.repositories.issues import IssuesRepository
from embry0.storage.repositories.jobs import JobsRepository

_COLUMNS = ("github_comment_id", "github_comment_seq")


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1
    return match[0][2]


def test_migration_62_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 62 in versions
    assert versions == list(range(1, len(versions) + 1))


def test_migration_62_adds_both_columns_idempotently() -> None:
    sql = _sql_for(62)
    for col in _COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {col}" in sql
        assert f"COMMENT ON COLUMN issue_inputs.{col}" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_issue_inputs_github_comment" in sql


def test_migration_62_backfills_nothing() -> None:
    """Existing rows genuinely have no render mapping — inventing one would
    re-create the very mis-targeting #253 removes."""
    sql = _sql_for(62)
    assert "UPDATE issue_inputs" not in sql


async def _seed_issue(db: DatabasePool) -> tuple[str, str]:
    issue_id = await IssuesRepository(db).create(title="t", repo="o/r")
    job_id = await JobsRepository(db).create(repo="o/r", task="t")
    return issue_id, job_id


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_columns_exist_and_default_to_null(db_with_migrations: DatabasePool) -> None:
    issue_id, job_id = await _seed_issue(db_with_migrations)
    repo = IssueInputsRepository(db_with_migrations)
    input_id = await repo.create(issue_id=issue_id, job_id=job_id, question="Q?")

    row = await repo.get(input_id)
    assert row is not None
    for col in _COLUMNS:
        assert row[col] is None


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_sequence_lookup_resolves_against_the_one_outstanding_comment(
    db_with_migrations: DatabasePool,
) -> None:
    """The unambiguous case: every pending question came from one comment, so
    position N means position N of the list the author is looking at."""
    issue_id, job_id = await _seed_issue(db_with_migrations)
    repo = IssueInputsRepository(db_with_migrations)

    q1 = await repo.create(issue_id=issue_id, job_id=job_id, question="Q1?")
    q2 = await repo.create(issue_id=issue_id, job_id=job_id, question="Q2?")
    await repo.set_github_comment_ref(q1, 200, 1)
    await repo.set_github_comment_ref(q2, 200, 2)

    first = await repo.get_by_github_comment_sequence(issue_id, 1)
    assert first is not None and first["id"] == q1
    second = await repo.get_by_github_comment_sequence(issue_id, 2)
    assert second is not None and second["id"] == q2


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_sequence_lookup_refuses_when_two_comments_are_outstanding(
    db_with_migrations: DatabasePool,
) -> None:
    """Comment 100 asked Q_old and it is still pending when comment 200 asks
    Q_new. Replying to either with "/answer 1:" is ambiguous — GitHub issue
    comments carry no in_reply_to — so it must resolve to nothing and get the
    "use the id" reply, not silently answer the other comment's question 1."""
    issue_id, job_id = await _seed_issue(db_with_migrations)
    repo = IssueInputsRepository(db_with_migrations)

    old_q = await repo.create(issue_id=issue_id, job_id=job_id, question="old Q?")
    await repo.set_github_comment_ref(old_q, 100, 1)
    new_q = await repo.create(issue_id=issue_id, job_id=job_id, question="new Q1?")
    await repo.set_github_comment_ref(new_q, 200, 1)

    assert await repo.get_by_github_comment_sequence(issue_id, 1) is None
    # The id still resolves — that is the recoverable path the reply names.
    row = await repo.get(new_q)
    assert row is not None and row["status"] == "pending"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_sequence_lookup_resolves_once_the_older_comment_is_fully_answered(
    db_with_migrations: DatabasePool,
) -> None:
    """The ambiguity is about questions still waiting, not comments ever
    posted: once comment 100's questions are answered, comment 200 is the
    only list anyone can still be replying to."""
    issue_id, job_id = await _seed_issue(db_with_migrations)
    repo = IssueInputsRepository(db_with_migrations)

    old_q = await repo.create(issue_id=issue_id, job_id=job_id, question="old Q?")
    await repo.set_github_comment_ref(old_q, 100, 1)
    new_q = await repo.create(issue_id=issue_id, job_id=job_id, question="new Q1?")
    await repo.set_github_comment_ref(new_q, 200, 1)
    await repo.answer(old_q, answer="done", answered_by="alex")

    row = await repo.get_by_github_comment_sequence(issue_id, 1)
    assert row is not None and row["id"] == new_q


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_sequence_lookup_refuses_when_a_blocking_question_has_no_mapping(
    db_with_migrations: DatabasePool,
) -> None:
    """The newest question comment failed to post, or its mapping write did.
    Either way there may be a list on the issue we cannot resolve against, so
    N must not fall through to the previous comment's question 1."""
    issue_id, job_id = await _seed_issue(db_with_migrations)
    repo = IssueInputsRepository(db_with_migrations)

    mapped = await repo.create(issue_id=issue_id, job_id=job_id, question="Q?")
    await repo.set_github_comment_ref(mapped, 100, 1)
    await repo.create(issue_id=issue_id, job_id=job_id, question="unmapped blocking Q?")

    assert await repo.get_by_github_comment_sequence(issue_id, 1) is None


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_unmapped_non_blocking_questions_do_not_block_the_positional_form(
    db_with_migrations: DatabasePool,
) -> None:
    """Only blocking questions are dispatched to GitHub, so an auto_answerable
    row left pending with no suggested answer is never a list the author could
    be reading — it must not make "/answer 1:" unusable."""
    issue_id, job_id = await _seed_issue(db_with_migrations)
    repo = IssueInputsRepository(db_with_migrations)

    q1 = await repo.create(issue_id=issue_id, job_id=job_id, question="Q1?")
    await repo.set_github_comment_ref(q1, 100, 1)
    await repo.create(
        issue_id=issue_id,
        job_id=job_id,
        question="nice to know?",
        importance="auto_answerable",
    )

    row = await repo.get_by_github_comment_sequence(issue_id, 1)
    assert row is not None and row["id"] == q1


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_sequence_lookup_skips_a_question_answered_elsewhere(
    db_with_migrations: DatabasePool,
) -> None:
    """#253 acceptance: the dashboard answered question 1, so "/answer 1:" on
    GitHub resolves to nothing — never to question 2."""
    issue_id, job_id = await _seed_issue(db_with_migrations)
    repo = IssueInputsRepository(db_with_migrations)

    q1 = await repo.create(issue_id=issue_id, job_id=job_id, question="Q1?")
    q2 = await repo.create(issue_id=issue_id, job_id=job_id, question="Q2?")
    await repo.set_github_comment_ref(q1, 300, 1)
    await repo.set_github_comment_ref(q2, 300, 2)
    await repo.answer(q1, answer="from the dashboard", answered_by="alex")

    assert await repo.get_by_github_comment_sequence(issue_id, 1) is None
    still_there = await repo.get_by_github_comment_sequence(issue_id, 2)
    assert still_there is not None and still_there["id"] == q2


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_sequence_lookup_returns_none_for_unrendered_questions(
    db_with_migrations: DatabasePool,
) -> None:
    """Pre-#253 rows carry no mapping and must stay unaddressable by position."""
    issue_id, job_id = await _seed_issue(db_with_migrations)
    repo = IssueInputsRepository(db_with_migrations)
    await repo.create(issue_id=issue_id, job_id=job_id, question="Q?")

    assert await repo.get_by_github_comment_sequence(issue_id, 1) is None
