"""Label-based cleanup for QA jobs.

Two label families to filter on:
  com.docker.compose.project=qa_<job_id>   -- spawned by `qa-compose` wrapper
  embry0.qa_job_id=<job_id>               -- spawned directly via `docker run`

Plus the per-job network qa-net-<job_id>.

Called from:
  - report_node, every attempt (whether the attempt succeeded or failed)
  - ContainerReaper, periodically, to sweep orphans from crashed jobs
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TypeVar

import structlog

from embry0.execution.runtime import AgentRuntime

logger = structlog.get_logger(__name__)

T = TypeVar("T")


async def cleanup_qa_resources(runtime: AgentRuntime, *, job_id: str) -> None:
    """Best-effort teardown of every container/network/volume for one QA job.

    Failures are logged and swallowed -- partial cleanup is better than
    aborting and leaving more orphans, and the next ContainerReaper sweep
    will catch anything missed.
    """

    async def _run(step: str, call: Awaitable[T], empty: T) -> T:
        try:
            return await call
        except RuntimeError as exc:
            logger.warning("qa_cleanup_step_failed", step=step, error=str(exc))
            return empty

    project_label = f"com.docker.compose.project=qa_{job_id}"
    job_label = f"embry0.qa_job_id={job_id}"

    # 1. Containers -- both label families.
    for label in (project_label, job_label):
        found = await _run(f"list containers {label}", runtime.list_containers(label=label, timeout=15), [])
        if found:
            await _run(f"remove containers {label}", runtime.remove_containers([c.id for c in found], timeout=15), None)

    # 2. Volumes -- both label families.
    for label in (project_label, job_label):
        vols = await _run(f"list volumes {label}", runtime.list_volumes(labels=[label], timeout=15), [])
        if vols:
            await _run(f"remove volumes {label}", runtime.remove_volumes(vols, timeout=15), None)

    # 3. The per-job network.
    await _run("remove network", runtime.remove_network(f"qa-net-{job_id}", timeout=15), None)

    logger.info("qa_cleanup_done", job_id=job_id)
