"""Label-based cleanup for QA jobs.

Two label families to filter on:
  com.docker.compose.project=qa_<job_id>   -- spawned by `qa-compose` wrapper
  embry0.qa_job_id=<job_id>               -- spawned directly via `docker run`

Plus the per-job network qa-net-<job_id>.

Called from:
  - release_sandbox_node, after every QA sub-task (passed or failed)
  - ContainerReaper, periodically, to sweep orphans from crashed jobs
  - IssueExecutor.cancel_job, through ``cleanup_cancelled_qa_job`` (#328)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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


async def cleanup_cancelled_qa_job(
    runtime: AgentRuntime,
    destroy_sandbox: Callable[[str], Awaitable[None]],
    *,
    job_id: str,
) -> None:
    """Release the QA resources of a job that was cancelled (#328).

    A cancelled graph never reaches the nodes that clean up after QA, so its
    fan-out sub-task sandboxes, their per-sub-task networks and anything the
    QA agent started inside DinD would linger until the reaper. Sub-task
    sandboxes are found by name — ``sandbox-<job_id>__<app>``, created for
    sub-job ``<job_id>__<app>`` — and destroyed through ``destroy_sandbox``
    (the sandbox manager, which also unenrols them from the proxies) before
    their networks are removed: a network with an attached container cannot
    be. Best-effort; never raises.
    """
    prefix = f"sandbox-{job_id}__"
    try:
        found = await runtime.list_containers(name=prefix, timeout=15)
    except Exception as exc:
        logger.warning("qa_cancel_cleanup_list_failed", job_id=job_id, error=str(exc))
        found = []
    # Docker's name filter is a substring match: keep the exact prefix only.
    subtasks = [c.name for c in found if c.name.startswith(prefix)]

    sub_job_ids = []
    for name in subtasks:
        try:
            await destroy_sandbox(name)
        except Exception as exc:
            logger.warning("qa_cancel_subtask_destroy_failed", job_id=job_id, sandbox=name, error=str(exc))
        sub_job_ids.append(f"{job_id}__{name[len(prefix) :]}")

    for qa_job_id in (job_id, *sub_job_ids):
        try:
            await cleanup_qa_resources(runtime, job_id=qa_job_id)
        except Exception as exc:
            logger.warning("qa_cancel_cleanup_failed", job_id=qa_job_id, error=str(exc))
