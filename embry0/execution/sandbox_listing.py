"""Which containers on the agent daemon are embry0 sandboxes (#620).

The reapers used to select by Docker's name filter, ``name=sandbox-``, which
matches the string anywhere in a name: any container on the daemon called
``…sandbox-…`` was a candidate for destruction. Every sandbox now carries
the ``embry0.role=sandbox`` label (#616), and that label is the selector.

Sandboxes created before the label existed have none, and a paused or
awaiting-input job can keep one for days. Those are still found by name,
but only when the name starts with ``sandbox-`` and the daemon says the
container was created before ``LABELLED_SINCE``. A container created after
that date without the label is not embry0's, whatever its name. Once no
deployment can hold a sandbox older than that date, the legacy branch finds
nothing and can be removed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from embry0.execution.docker_client import SANDBOX_ROLE_LABEL
from embry0.execution.runtime import AgentRuntime
from embry0.execution.runtime.protocol import ContainerSummary

logger = structlog.get_logger(__name__)

SANDBOX_NAME_PREFIX = "sandbox-"
# The day after #616 (which added the label) merged. Every sandbox created
# from then on by a release carrying #616 is labelled.
LABELLED_SINCE = datetime(2026, 10, 10, tzinfo=UTC)


def parse_ps_created(created_at: str) -> datetime | None:
    """``docker ps``'s ``CreatedAt`` string as an aware datetime, or None if unparseable.

    Docker emits ``<timestamp> <offset> <tz_abbrev>``, e.g.
    ``2026-04-28 07:05:11 +0000 UTC`` (CLI with TZ=UTC) or
    ``2026-04-27 15:32:08 -0400 EDT``. The trailing abbreviation is dropped;
    ``%z`` reads the offset.
    """
    parts = created_at.rsplit(" ", 1)
    cleaned = parts[0] if len(parts) == 2 and parts[1].isalpha() else created_at
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def _is_legacy_sandbox(container: ContainerSummary) -> bool:
    if not container.name.startswith(SANDBOX_NAME_PREFIX):
        return False
    created = parse_ps_created(container.created_at)
    return created is not None and created < LABELLED_SINCE


async def list_sandbox_containers(runtime: AgentRuntime, *, timeout: int = 15) -> list[ContainerSummary]:
    """Every embry0 sandbox on the daemon, running or stopped.

    Raises what ``list_containers`` raises; callers already treat a failed
    listing as "reap nothing".
    """
    labelled = await runtime.list_containers(label=SANDBOX_ROLE_LABEL, timeout=timeout)
    seen = {c.id for c in labelled}
    legacy = [
        c
        for c in await runtime.list_containers(name=SANDBOX_NAME_PREFIX, timeout=timeout)
        if c.id not in seen and _is_legacy_sandbox(c)
    ]
    if legacy:
        logger.info("sandbox_unlabelled_legacy_found", containers=[c.name for c in legacy])
    return [*labelled, *legacy]
