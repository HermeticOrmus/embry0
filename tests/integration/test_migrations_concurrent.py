"""Two orchestrator replicas migrating one database must not race (#249)."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from structlog.testing import capture_logs

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS, run_migrations
from tests.fixtures.postgres import postgres_unavailable


@pytest.fixture
async def scratch_database_url() -> AsyncIterator[str]:
    """Create an empty throwaway database and drop it afterwards.

    Deliberately NOT the shared ``setup_database`` fixture: this test needs a
    database on which *no* migration has run yet, so both runners start from
    an empty ledger and actually contend for the advisory lock.
    """
    base_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://embry0:embry0@localhost:5432/embry0_integration_test",
    )
    sys_url = base_url.rsplit("/", 1)[0] + "/postgres"
    db_name = f"embry0_migrate_race_{uuid.uuid4().hex[:12]}"

    try:
        sys_conn = await asyncpg.connect(sys_url)
    except (OSError, asyncpg.PostgresError) as exc:
        postgres_unavailable(exc)

    try:
        await sys_conn.execute(f"CREATE DATABASE {db_name}")
        yield base_url.rsplit("/", 1)[0] + f"/{db_name}"
    finally:
        await sys_conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        await sys_conn.close()


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_concurrent_run_migrations_apply_each_migration_once(scratch_database_url: str) -> None:
    """Two concurrent runners both succeed, and the ledger holds one row each.

    Without the advisory lock both passes read the same empty applied set and
    replay the same migrations: the duplicate ledger INSERT trips the
    ``embry0_migrations`` primary key (or the non-idempotent DDL fails first)
    and one runner raises.
    """
    replicas = [DatabasePool(scratch_database_url, min_size=1, max_size=2) for _ in range(2)]
    for db in replicas:
        await db.connect()

    try:
        with capture_logs() as logs:
            # Neither pass may raise: unserialised, one of them trips the
            # ledger primary key or the non-idempotent DDL.
            assert await asyncio.gather(*(run_migrations(db) for db in replicas)) == [None, None]

        rows = await replicas[0].fetch("SELECT version FROM embry0_migrations")
        assert {row["version"] for row in rows} == {version for version, _, _ in MIGRATIONS}
        assert len(rows) == len(MIGRATIONS)

        # Both runners held the lock, and the second one only got it after the
        # first had finished applying — the passes queued, they did not
        # interleave.
        events = [entry["event"] for entry in logs]
        acquisitions = [i for i, event in enumerate(events) if event == "migrations_lock_acquired"]
        assert len(acquisitions) == 2
        assert acquisitions[1] > max(i for i, event in enumerate(events) if event == "migration_applied")
    finally:
        for db in replicas:
            await db.close()
