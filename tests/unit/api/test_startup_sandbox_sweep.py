"""The start-up sweep removes sandboxes the previous process left behind, not paused ones (#484).

It used to destroy every ``sandbox-*`` container on every start. A paused or
awaiting-input job's sandbox is not an orphan: its checkpoint names the
container and the resume reuses it. Every deploy turned every paused job into
one that could not be resumed (2026-10-05: four sandboxes, two deploys).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import embry0.api.app as app_module
from embry0.api.app import _clean_orphaned_sandboxes
from embry0.execution.runtime.protocol import ContainerSummary


class _Runtime:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    async def list_containers(self, *, name=None, timeout=15, **_kwargs):
        assert name == "sandbox-"
        return [ContainerSummary(f"id{i}", n, "", "", "") for i, n in enumerate(self.names)]


def _jobs(active: set[str] | None = None, error: Exception | None = None) -> MagicMock:
    repo = MagicMock()
    if error is not None:
        repo.fetch_active_sandbox_containers = AsyncMock(side_effect=error)
    else:
        repo.fetch_active_sandbox_containers = AsyncMock(return_value=set(active or ()))
    return repo


def _sandbox() -> MagicMock:
    mgr = MagicMock()
    mgr.destroy = AsyncMock()
    return mgr


@pytest.mark.asyncio
async def test_a_paused_jobs_sandbox_survives_the_start_sweep():
    sandbox = _sandbox()
    runtime = _Runtime(["sandbox-job-paused", "sandbox-job-killed", "sandbox-job-waiting"])

    await _clean_orphaned_sandboxes(runtime, sandbox, _jobs({"sandbox-job-paused", "sandbox-job-waiting"}))

    destroyed = [call.args[0] for call in sandbox.destroy.await_args_list]
    assert destroyed == ["sandbox-job-killed"], f"only the orphan may be destroyed, destroyed {destroyed}"


@pytest.mark.asyncio
async def test_without_the_active_set_the_sweep_destroys_nothing():
    """Fail closed, as the reaper does: a sandbox left behind costs disk until the
    reaper; a paused sandbox destroyed costs the job."""
    sandbox = _sandbox()
    runtime = _Runtime(["sandbox-job-paused", "sandbox-job-killed"])

    await _clean_orphaned_sandboxes(runtime, sandbox, _jobs(error=RuntimeError("database unavailable")))

    sandbox.destroy.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_a_jobs_repository_the_sweep_destroys_nothing():
    sandbox = _sandbox()

    await _clean_orphaned_sandboxes(_Runtime(["sandbox-job-paused"]), sandbox, None)

    sandbox.destroy.assert_not_awaited()


def test_start_up_passes_the_jobs_repository_to_the_sweep():
    """The repository is what spares paused sandboxes; production must pass it."""
    src = Path(app_module.__file__).read_text()
    call = re.search(r"await _clean_orphaned_sandboxes\(([^)]*)\)", src)
    assert call, "lifespan no longer runs the start-up sandbox sweep"
    assert "jobs_repo" in call.group(1), f"the sweep is called without the jobs repository: {call.group(0)}"
    # _init_app_state sets app.state.jobs_repo, and lifespan calls it after the sweep:
    # reading it there raises AttributeError at start-up (the DB-backed
    # test_lifespan_wiring.py tests boot the real lifespan and caught exactly that).
    assert "app.state.jobs_repo" not in call.group(1), f"app.state.jobs_repo is not set yet: {call.group(0)}"
    assert src.index("await _clean_orphaned_sandboxes(") < src.index("await _init_app_state("), (
        "if the sweep moved after _init_app_state, revisit which repository it receives"
    )
