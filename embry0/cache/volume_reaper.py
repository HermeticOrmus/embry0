"""Startup + periodic sweep of leaked embry0 volumes inside DinD (#274).

Per-job QA volumes are normally removed when their job goes terminal
(``SharedVolumeManager.destroy_for_job``). This sweep is the backstop for the
paths that cleanup cannot cover — an orchestrator crash or redeploy mid-job
(the job is failed as orphaned at the next boot), a paused job the
ContainerReaper expires, a removal that failed because a container still held
the volume.

The reaper only gathers facts; ``volume_ownership.decide_volume`` makes every
keep/remove call, starting with whether the volume was stamped by this
instance at all. Removal never uses ``--force``, so Docker itself refuses a
volume that a container still mounts. With ``dry_run`` the sweep logs
``volume_reaper_would_remove`` and touches nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from embry0.cache.volume_ownership import (
    LABEL_INSTANCE,
    VolumeInfo,
    decide_volume,
    owning_job_id,
)
from embry0.metrics import NULL_METRICS, LoopMetrics

if TYPE_CHECKING:
    from embry0.execution.runtime import AgentRuntime
    from embry0.storage.repositories.qa_volume_state import QAVolumeStateRepository

logger = structlog.get_logger(__name__)

# Substring filter that pre-selects pre-label per-job volumes; the exact-name
# check in volume_ownership is what actually grants ownership.
_LEGACY_NAME_FILTER = "embry0-qa-vol-job-"
_INSPECT_CHUNK = 50


@dataclass(slots=True)
class SweepResult:
    removed: list[str] = field(default_factory=list)
    would_remove: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    kept: int = 0


def _parse_created_at(raw: Any) -> datetime | None:
    """``docker volume inspect`` emits RFC3339 seconds, e.g. 2026-07-19T00:14:06Z."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


class VolumeReaper:
    """Background task that removes volumes whose owning job is terminal or unknown."""

    def __init__(
        self,
        runtime: AgentRuntime,
        jobs_repo: Any,
        *,
        state_repo: QAVolumeStateRepository | None,
        instance_id: str | None,
        interval_seconds: int = 3600,
        dry_run: bool = False,
        adopt_legacy_names: bool = False,
        unknown_owner_grace_minutes: int = 60,
        metrics: LoopMetrics | None = None,
    ) -> None:
        self._metrics = metrics or NULL_METRICS
        self._runtime = runtime
        self._jobs_repo = jobs_repo
        # Rows in qa_volume_state describe a volume; a removed volume's row
        # goes with it (#328). Required, so production cannot forget it.
        self._state_repo = state_repo
        # Identity of the database jobs_repo reads (storage.instance_id). None
        # — it could not be resolved — leaves every labelled volume alone.
        self._instance_id = instance_id
        self._interval = interval_seconds
        self._dry_run = dry_run
        self._adopt_legacy_names = adopt_legacy_names
        self._unknown_owner_grace = timedelta(minutes=unknown_owner_grace_minutes)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background task: one sweep now, then one per interval."""
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "volume_reaper_started",
            instance_id=self._instance_id,
            interval_seconds=self._interval,
            dry_run=self._dry_run,
            adopt_legacy_names=self._adopt_legacy_names,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.sweep()
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("volume_reaper_error")
                await asyncio.sleep(self._interval)

    async def _list_volumes(self) -> list[VolumeInfo]:
        # Only this instance's volumes are even listed; decide_volume checks
        # the label again, so this is an optimisation, not the safety rule.
        names: list[str] = []
        if self._instance_id:
            listed = await self._runtime.list_volumes(labels=[f"{LABEL_INSTANCE}={self._instance_id}"], timeout=15)
            names += [n for n in listed if n not in names]
        if self._adopt_legacy_names:
            listed = await self._runtime.list_volumes(name=_LEGACY_NAME_FILTER, timeout=15)
            names += [n for n in listed if n not in names]

        volumes: list[VolumeInfo] = []
        for i in range(0, len(names), _INSPECT_CHUNK):
            for entry in await self._runtime.inspect_volumes(names[i : i + _INSPECT_CHUNK], timeout=30):
                volumes.append(
                    VolumeInfo(
                        name=entry.name,
                        labels=entry.labels,
                        created_at=_parse_created_at(entry.created_at),
                    )
                )
        return volumes

    async def sweep(self) -> SweepResult:
        """One pass. Never raises; any lookup failure means nothing is removed."""
        result = SweepResult()
        try:
            volumes = await self._list_volumes()
        except Exception as exc:
            logger.warning("volume_reaper_list_failed", error=str(exc))
            self._metrics.volume_reaper_sweep("list_failed")
            return result
        if not volumes:
            return result

        owners = {owning_job_id(v, adopt_legacy_names=self._adopt_legacy_names) for v in volumes}
        try:
            statuses = await self._jobs_repo.fetch_statuses(sorted(o for o in owners if o))
        except Exception:
            # Fail-closed: if we can't tell which jobs are live, don't reap.
            logger.warning("volume_reaper_status_fetch_failed", exc_info=True)
            self._metrics.volume_reaper_sweep("status_fetch_failed")
            return result

        now = datetime.now(UTC)
        for vol in volumes:
            decision = decide_volume(
                vol,
                instance_id=self._instance_id,
                job_statuses=statuses,
                now=now,
                unknown_owner_grace=self._unknown_owner_grace,
                adopt_legacy_names=self._adopt_legacy_names,
            )
            if not decision.remove:
                result.kept += 1
                self._metrics.volume_reaper_volume("kept", str(decision.reason))
                continue
            fields = {"volume": vol.name, "job_id": decision.job_id, "reason": str(decision.reason)}
            if self._dry_run:
                result.would_remove.append(vol.name)
                self._metrics.volume_reaper_volume("would_remove", str(decision.reason))
                logger.info("volume_reaper_would_remove", **fields)
                continue
            try:
                # No --force: Docker refuses a volume a container still mounts.
                await self._runtime.remove_volumes([vol.name], timeout=30)
            except Exception as exc:
                result.failed.append(vol.name)
                self._metrics.volume_reaper_volume("failed", str(decision.reason))
                logger.warning("volume_reaper_remove_failed", error=str(exc), **fields)
                continue
            result.removed.append(vol.name)
            self._metrics.volume_reaper_volume("removed", str(decision.reason))
            logger.info("volume_reaper_removed", **fields)
            if self._state_repo is not None:
                try:
                    await self._state_repo.delete_by_volume_name(vol.name)
                except Exception as exc:
                    logger.warning("volume_reaper_state_delete_failed", error=str(exc), **fields)

        self._metrics.volume_reaper_sweep("ok")
        logger.info(
            "volume_reaper_sweep_done",
            dry_run=self._dry_run,
            removed=len(result.removed),
            would_remove=len(result.would_remove),
            failed=len(result.failed),
            kept=result.kept,
        )
        return result
