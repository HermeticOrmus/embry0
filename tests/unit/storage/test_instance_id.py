"""Instance id stamped on DinD volumes (#274)."""

import re

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.instance_id import derive_instance_id, fetch_instance_id


def test_derive_is_deterministic_and_label_safe():
    a = derive_instance_id(7687464636179496994, "embry0")
    assert a == derive_instance_id("7687464636179496994", "embry0")
    assert re.fullmatch(r"[0-9a-f]{16}", a)


def test_derive_separates_clusters_and_databases():
    base = derive_instance_id(1, "embry0")
    assert derive_instance_id(2, "embry0") != base
    assert derive_instance_id(1, "embry0_staging") != base


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_fetch_is_stable_for_one_database(db_with_migrations: DatabasePool):
    first = await fetch_instance_id(db_with_migrations)
    assert re.fullmatch(r"[0-9a-f]{16}", first)
    assert await fetch_instance_id(db_with_migrations) == first
