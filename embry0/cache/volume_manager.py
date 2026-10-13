"""Docker volume manager for the Phase-2 shared-volume cache layer.

Volume naming convention:
  per-job  → embry0-qa-vol-<job_id>
  per-repo → embry0-qa-vol-<sanitized-repo>

per-org was rejected as a scope in RAV-1061: a shared volume holds ONE
repo's tree and ONE lockfile sha, so cross-repo sharing is not expressible
here — qa.yaml now refuses the value at parse time.

Sanitization replaces '/' and ':' with '-' so names are valid Docker volume IDs.
"""

from __future__ import annotations

from collections.abc import Awaitable
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

import structlog

from embry0.cache.volume_ownership import LABEL_INSTANCE, LABEL_JOB_ID, volume_labels
from embry0.storage.repositories.qa_volume_state import QAVolumeStateRepository

if TYPE_CHECKING:
    from embry0.execution.runtime import AgentRuntime

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class VolumeScope(StrEnum):
    PER_JOB = "per-job"
    PER_REPO = "per-repo"


def _sanitize(name: str) -> str:
    return name.replace("/", "-").replace(":", "-")


def volume_name_for(scope: VolumeScope, scope_key: str) -> str:
    if scope is VolumeScope.PER_JOB or scope is VolumeScope.PER_REPO:
        return f"embry0-qa-vol-{_sanitize(scope_key)}"
    raise ValueError(f"unknown scope: {scope!r}")


class SharedVolumeManager:
    """Wraps the Docker volume API + state-tracking repo."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        state_repo: QAVolumeStateRepository,
        instance_id: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.state_repo = state_repo
        # Stamped on every volume created (#274). None leaves volumes without
        # an instance label, which the reaper will never touch.
        self.instance_id = instance_id

    async def ensure(self, *, scope: VolumeScope, scope_key: str) -> str:
        """Create the volume if missing, record state, return its name."""
        name = volume_name_for(scope, scope_key)
        # Idempotency: check if volume already exists. Docker's name filter
        # is a substring match, so compare whole names (#328).
        if name in await self.runtime.list_volumes(name=name, timeout=10):
            await self.state_repo.upsert(
                scope=str(scope),
                scope_key=scope_key,
                volume_name=name,
                last_warmed_sha=None,
            )
            return name

        # #274: ownership labels are how the reaper tells an embry0 volume
        # (and its owning job) from anything else living in DinD.
        await self.runtime.create_volume(
            name,
            labels=volume_labels(str(scope), scope_key, instance_id=self.instance_id),
            timeout=10,
        )
        await self.state_repo.upsert(
            scope=str(scope),
            scope_key=scope_key,
            volume_name=name,
            last_warmed_sha=None,
        )
        logger.info("qa_shared_volume_created", scope=str(scope), name=name)
        return name

    async def destroy(self, volume_name: str) -> None:
        """Remove the volume. Failures are logged, not raised."""
        try:
            await self.runtime.remove_volumes([volume_name], force=True, timeout=10)
        except Exception as exc:
            logger.warning("qa_shared_volume_destroy_failed", name=volume_name, error=str(exc))
            return
        await self._forget(volume_name)

    async def _forget(self, volume_name: str) -> None:
        """Drop the state row of a removed volume (#328). Best-effort."""
        try:
            await self.state_repo.delete_by_volume_name(volume_name)
        except Exception as exc:
            logger.warning("qa_volume_state_delete_failed", name=volume_name, error=str(exc))

    async def destroy_for_job(self, job_id: str) -> list[str]:
        """Remove every volume owned by ``job_id``; return the names removed.

        Called once the job is terminal (#274). Finds the volume by its
        deterministic per-job name and by the ``embry0.job_id`` label, force-
        removes any container still mounting it (a cancelled fan-out leaves
        sub-task sandboxes behind — they belong to the same dead job), then
        removes the volume. Best-effort: failures are logged, never raised,
        and the periodic ``VolumeReaper`` sweep retries whatever is left.
        """

        async def _run(step: str, call: Awaitable[T]) -> T | None:
            try:
                return await call
            except Exception as exc:
                logger.warning("qa_job_volume_cleanup_step_failed", job_id=job_id, step=step, error=str(exc))
                return None

        per_job_name = volume_name_for(VolumeScope.PER_JOB, job_id)
        # Docker's name filter is a substring match — keep the exact name only.
        by_name = await _run("list by name", self.runtime.list_volumes(name=per_job_name, timeout=15))
        label_filters = [f"{LABEL_JOB_ID}={job_id}"]
        if self.instance_id:
            # Filters AND together: another instance's job ids are not ours.
            label_filters.append(f"{LABEL_INSTANCE}={self.instance_id}")
        by_label = await _run("list by label", self.runtime.list_volumes(labels=label_filters, timeout=15))
        names = [n for n in (by_name or []) if n == per_job_name]
        names += [n for n in (by_label or []) if n not in names]

        removed: list[str] = []
        for name in names:
            holders = await _run(f"list holders of {name}", self.runtime.list_containers(volume=name, timeout=15))
            if holders:
                await _run(
                    f"remove holders of {name}", self.runtime.remove_containers([c.id for c in holders], timeout=15)
                )
            if await _run(f"remove {name}", self._removed(name)) is not None:
                removed.append(name)
                await self._forget(name)
        if removed:
            logger.info("qa_job_volumes_removed", job_id=job_id, volumes=removed)
        return removed

    async def _removed(self, volume: str) -> bool:
        # No force: the daemon refuses a volume a container still mounts.
        await self.runtime.remove_volumes([volume], timeout=15)
        return True
