"""Migration 91 — drop pipeline_templates.agent_models, which nothing read (#468)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS


def test_migration_91_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 91 in versions, "migration 91 (drop pipeline_templates.agent_models, #468) is missing"
    assert versions == list(range(1, len(versions) + 1))


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_the_column_is_gone_and_templates_still_round_trip(db_with_migrations: DatabasePool) -> None:
    from embry0.storage.repositories.pipeline_templates import PipelineTemplatesRepository

    cols = await db_with_migrations.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'pipeline_templates'"
    )
    assert "agent_models" not in {c["column_name"] for c in cols}

    repo = PipelineTemplatesRepository(db_with_migrations)
    graph = {"nodes": [{"node_id": "d", "agent_type": "developer", "config": {"model": "m"}}], "edges": []}
    created = await repo.create(name="round-trip", graph_definition=graph)
    copy = await repo.duplicate(created["id"], new_name="round-trip-copy")
    assert copy["graph_definition"] == graph
