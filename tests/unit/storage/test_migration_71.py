"""Migration 71 — traces.error_code / stderr_tail, and the backfill of
unambiguous historical job error codes (#281)."""

import pytest

from embry0.safety.error_codes import ErrorCode
from embry0.storage.database import DatabasePool
from embry0.storage.migrations import runner
from embry0.storage.migrations.runner import MIGRATIONS


def _sql() -> str:
    match = [sql for v, _, sql in MIGRATIONS if v == 71]
    assert len(match) == 1
    return match[0]


def test_migration_71_is_appended_in_order() -> None:
    """71 sits after every earlier migration and before every later one.

    This was "71 is the last entry", which could only hold until the next
    migration landed — #299's 72 is that migration. Appended-in-order is
    the invariant the original assertion was reaching for, and it survives.
    """
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions.count(71) == 1
    position = versions.index(71)
    assert all(v < 71 for v in versions[:position])
    assert all(v > 71 for v in versions[position + 1 :])


def test_backfill_only_writes_codes_this_build_knows() -> None:
    assert {code for _, code in runner._M71_BACKFILL_RULES} <= {c.value for c in ErrorCode}
    assert ErrorCode.UNKNOWN.value not in {code for _, code in runner._M71_BACKFILL_RULES}


async def _failed(db: DatabasePool, job_id: str, message: str | None, code: str | None = "ERR_UNKNOWN") -> None:
    await db.execute(
        "INSERT INTO jobs (job_id, repo, task, status, error_message, error_code) VALUES ($1, 'o/r', 't', 'failed', $2, $3)",
        job_id,
        message,
        code,
    )


async def _code(db: DatabasePool, job_id: str) -> str | None:
    value: str | None = await db.fetchval("SELECT error_code FROM jobs WHERE job_id = $1", job_id)
    return value


# One row per production cluster (messages copied from the 2026-09-20 data,
# with synthetic app names).
_CLUSTERS = [
    ("Separator is found, but chunk is longer than limit", "ERR_AGENT_OUTPUT_OVERFLOW"),
    ("Separator is not found, and chunk exceed the limit", "ERR_AGENT_OUTPUT_OVERFLOW"),
    ("analysis: Claude Code returned an error result: Reached maximum number of turns (40)", "ERR_AGENT_MAX_TURNS"),
    (
        "developer: Command failed with exit code 1 (exit code: 1)\nError output: Check stderr output for details",
        "ERR_AGENT_PROCESS_FAILED",
    ),
    ("Abandoned after 3 retries", "ERR_ABANDONED"),
    ("triage_malformed: Invalid JSON: Expecting value: line 1 column 1 (char 0)", "ERR_TRIAGE_MALFORMED"),
    (
        "At key 'qa': Can receive only one value per step. Use an Annotated key to handle multiple values.",
        "ERR_STATE_CONCURRENT_UPDATE",
    ),
    (
        "Developer agent did not push branch 'embry0/x' to origin. This breaks the QA handoff",
        "ERR_DEV_BRANCH_NOT_PUSHED",
    ),
    ("[Errno 7] Argument list too long", "ERR_SANDBOX_ARGV_TOO_LONG"),
    ("qa_failure_unresolved: triage_qa_failure_action_invalid_payload", "ERR_TRIAGE_QA_ACTION_INVALID"),
    (
        'null value in column "issue_id" of relation "issue_inputs" violates not-null constraint\nDETAIL: x',
        "ERR_STORAGE",
    ),
    ("QA: 0/1 apps passed — failed: web", "ERR_QA_FAILED"),
    ("QA: 0/3 apps passed — failed: admin,api; inconclusive: docs", "ERR_QA_FAILED"),
    ("QA: 0/1 apps passed — inconclusive: web", "ERR_QA_INCONCLUSIVE"),
    ("QA: 0/1 apps passed — infra: acme-course", "ERR_QA_INFRA"),
    ("QA: 1/3 apps passed — failed: web; infra: admin,docs", "ERR_QA_INFRA"),
    ("QA final_status=pending", "ERR_QA_NO_VERDICT"),
    ("sandbox profile capability check failed: app 'x': profile 'slim' has no browser", "ERR_QA_CONFIG_INVALID"),
    (
        "unknown conditional-criteria group(s) in force_conditional_groups: ['pricing'] — known groups: []",
        "ERR_QA_CONFIG_INVALID",
    ),
]


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_backfill_codes_every_production_cluster_and_is_idempotent(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    for i, (message, _) in enumerate(_CLUSTERS):
        # Half the historical rows carried ERR_UNKNOWN, the QA ones NULL.
        await _failed(db, f"j-{i}", message, "ERR_UNKNOWN" if i % 2 else None)

    for _ in range(2):  # the second pass must change nothing
        await db.execute(_sql())
        for i, (_, expected) in enumerate(_CLUSTERS):
            assert await _code(db, f"j-{i}") == expected, _CLUSTERS[i][0]


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_backfill_leaves_what_it_cannot_decide(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    # Two different causes in one message (job-080d33928cbd): not ours to pick.
    await _failed(
        db,
        "j-ambiguous",
        "Developer agent did not push branch 'b' to origin.; developer: Command failed with exit code 1",
    )
    await _failed(db, "j-novel", "something nobody has clustered")
    await _failed(db, "j-null-msg", None)
    await _failed(db, "j-null-novel", "something else", None)
    # Never relabel a row that already has a real code, or one that did not fail.
    await _failed(db, "j-coded", "Abandoned after 3 retries", "ERR_ORPHANED")
    await db.execute(
        "INSERT INTO jobs (job_id, repo, task, status, error_message) VALUES ('j-partial', 'o/r', 't', 'partial', 'QA: 1/2 apps passed — failed: web')"
    )

    await db.execute(_sql())

    assert await _code(db, "j-ambiguous") == "ERR_UNKNOWN"
    assert await _code(db, "j-novel") == "ERR_UNKNOWN"
    assert await _code(db, "j-null-msg") == "ERR_UNKNOWN"
    assert await _code(db, "j-null-novel") is None
    assert await _code(db, "j-coded") == "ERR_ORPHANED"
    assert await _code(db, "j-partial") is None


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_backfill_does_not_touch_updated_at_and_rearms_the_trigger(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    await _failed(db, "j-old", "Abandoned after 3 retries")
    await db.execute("ALTER TABLE jobs DISABLE TRIGGER trg_jobs_updated_at")
    await db.execute("UPDATE jobs SET updated_at = '2026-08-01T00:00:00Z' WHERE job_id = 'j-old'")
    await db.execute("ALTER TABLE jobs ENABLE TRIGGER trg_jobs_updated_at")
    before = await db.fetchval("SELECT updated_at FROM jobs WHERE job_id = 'j-old'")

    await db.execute(_sql())

    assert await _code(db, "j-old") == "ERR_ABANDONED"
    assert await db.fetchval("SELECT updated_at FROM jobs WHERE job_id = 'j-old'") == before
    assert await db.fetchval("SELECT tgenabled::text FROM pg_trigger WHERE tgname = 'trg_jobs_updated_at'") == "O"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_failure_mid_migration_cannot_leave_the_trigger_disabled(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        with pytest.raises(Exception, match="division by zero"):
            async with conn.transaction():
                await conn.execute(_sql().split("WITH rules")[0] + "SELECT 1/0;")
    assert await db.fetchval("SELECT tgenabled::text FROM pg_trigger WHERE tgname = 'trg_jobs_updated_at'") == "O"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_traces_gain_the_failure_columns(db_with_migrations: DatabasePool) -> None:
    cols = await db_with_migrations.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'traces' AND column_name IN ('error_code', 'stderr_tail')"
    )
    assert {r["column_name"] for r in cols} == {"error_code", "stderr_tail"}
