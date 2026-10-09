"""Identity of the database this orchestrator records its jobs in (#274).

The DinD volume reaper treats "this volume's job has no ``jobs`` row" as
grounds for removal. That inference only holds against the database that was
recording jobs when the volume was created, so every volume is stamped with
this id and the reaper ignores any volume whose stamp differs.

Derived, not configured: PostgreSQL's ``system_identifier`` is minted by
``initdb`` and is unique per cluster, and the database name separates two
stacks that share one cluster. It is therefore stable across restarts,
redeploys and an in-place ``psql`` restore, and differs for an empty or
rebuilt cluster, a second stack, or a ``DATABASE_URL`` pointed at the wrong
place — exactly the cases in which a missing row proves nothing. A cluster
rebuilt by ``initdb`` (including ``pg_upgrade``) gets a new id; volumes
stamped with the old one are then left alone for good.
"""

from __future__ import annotations

import hashlib

from embry0.storage.database import DatabasePool


def derive_instance_id(system_identifier: int | str, database: str) -> str:
    """Short, label-safe digest of the cluster identifier and database name."""
    return hashlib.sha256(f"{system_identifier}:{database}".encode()).hexdigest()[:16]


async def fetch_instance_id(db: DatabasePool) -> str:
    row = await db.fetchrow("SELECT system_identifier, current_database() AS database FROM pg_control_system()")
    assert row is not None, "pg_control_system() always returns one row"
    return derive_instance_id(row["system_identifier"], row["database"])
