"""Concurrent issue-less jobs never share a working branch (#558).

A job with no issue derived ``embry0/unknown-<first 30 chars of the task>``, so
two jobs whose tasks begin the same way derived ONE branch. The #478 probe
only helps when origin already holds the name, and two jobs dispatched
together both probe before either pushes: both were told to create the same
branch, and the second developer force-pushed over the first job's commit.
An issue-less job's branch now carries its job id, so it is its own from the
start and needs no probe.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from embry0.workflows.issue_to_pr.nodes._developer import _developer_working_branch

TASK = "Deploy proof job 2026-10-08: append one proof line"
FIRST, SECOND = "job-bc25a9c3ded1", "job-2af313479fa5"


def _shared() -> str:
    """The name every issue-less job with this task derived before #558."""
    from embry0.orchestration.branch_names import shared_branch_name

    return shared_branch_name(issue_id=None, task=TASK)


class _Origin:
    """One remote both sandboxes push to: branch -> the job whose commit it holds."""

    def __init__(self, heads: dict[str, str] | None = None) -> None:
        self.heads = dict(heads or {})
        self.overwritten: list[tuple[str, str, str]] = []

    def push(self, branch: str, job_id: str) -> None:
        holder = self.heads.get(branch)
        if holder is not None and holder != job_id:
            self.overwritten.append((branch, holder, job_id))
        self.heads[branch] = job_id


class _Sandbox:
    """Answers the probe's ``git ls-remote`` from the shared origin."""

    def __init__(self, origin: _Origin) -> None:
        self.origin = origin
        self.commands: list[list[str]] = []

    def build_exec_cmd(self, container_id: str, cmd: list[str]) -> list[str]:
        return ["docker", "exec", container_id, *cmd]

    async def run_cmd(self, cmd: list[str], timeout: int = 60) -> str:
        self.commands.append(cmd)
        if "ls-remote" in cmd:
            branch = cmd[-1]
            holder = self.origin.heads.get(branch)
            return f"{(holder or '').ljust(40, '0')}\trefs/heads/{branch}\n" if holder else ""
        return ""


def _state(job_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "issue_id": None,
        "repo": "acme/backend",
        "task": TASK,
        "sandbox_container_id": f"C-{job_id}",
        "agent_outputs": [],
        "errors": [],
        "pipeline_config": {"agent_skills": {"developer": []}, "budget_usd": 50.0},
        **extra,
    }


def _told_to_create(prompt: str) -> str:
    match = re.search(r"1\. Create branch: (\S+)", prompt)
    assert match, f"the developer was not told which branch to create:\n{prompt}"
    return match.group(1)


async def _run_together(origin: _Origin, states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run one developer node per state at once.

    Every agent waits until all of them have started, so every job has
    resolved its branch (and probed origin, if it probes) before any of them
    pushes: the race of #558. Each agent then pushes the branch it was told
    to create and reports it.
    """
    import embry0.workflows.issue_to_pr.nodes as nodes

    started = 0
    everyone_started = asyncio.Event()

    async def _agent(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal started
        started += 1
        if started == len(states):
            everyone_started.set()
        await asyncio.wait_for(everyone_started.wait(), timeout=5)
        job_id = kwargs["state"]["job_id"]
        branch = _told_to_create(kwargs["prompt"])
        origin.push(branch, job_id)
        output = f'Done.\n{{"pr_url": null, "branch": "{branch}", "summary": "s", "files_changed": []}}'
        return {
            "agent_outputs": [{"agent_type": "developer", "is_error": False, "output": output}],
            "events": [],
            "total_cost_usd": 0.01,
        }

    sessions = AsyncMock()
    sessions.get = AsyncMock(return_value=None)

    async def _one(state: dict[str, Any]) -> dict[str, Any]:
        command = await nodes.developer_node(
            state,
            {
                "configurable": {
                    "agent_runner": object(),
                    "credentials": {},
                    "agent_sessions_repo": sessions,
                    "docker": _Sandbox(origin),
                }
            },
        )
        return command.update

    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=AsyncMock(side_effect=_agent)),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
        patch("embry0.workflows.issue_to_pr.nodes._verify_branch_pushed", new=AsyncMock(return_value=True)),
    ):
        return list(await asyncio.gather(*(_one(state) for state in states)))


@pytest.mark.asyncio
async def test_two_concurrent_issueless_jobs_with_one_task_prefix_get_two_branches() -> None:
    origin = _Origin()

    first, second = await _run_together(origin, [_state(FIRST), _state(SECOND)])

    assert first["branch_name"] != second["branch_name"], (
        f"two concurrent issue-less jobs with one task prefix must never share a branch: "
        f"both were given {first['branch_name']!r}"
    )
    assert origin.overwritten == [], f"one job's push overwrote another job's branch: {origin.overwritten}"
    assert origin.heads == {first["branch_name"]: FIRST, second["branch_name"]: SECOND}, origin.heads


@pytest.mark.asyncio
async def test_an_issueless_job_keeps_its_own_branch_even_when_origin_already_holds_it() -> None:
    """Its name carries its job id, so a branch of that name on origin is its own earlier push.

    Probing it would name the job away from its own work (`<own>-<id>`) and
    tell its developer the branch belongs to an earlier job.
    """
    own = f"{_shared()}-bc25a9c3"
    origin = _Origin({own: FIRST})
    sandbox = _Sandbox(origin)

    branch, taken = await _developer_working_branch(_state(FIRST), {"docker": sandbox})

    assert (branch, taken) == (own, None), f"an issue-less job must keep its own branch: {branch!r}, {taken!r}"
    assert not any("ls-remote" in cmd for cmd in sandbox.commands), (
        f"a name only this job can hold needs no probe: {sandbox.commands}"
    )


@pytest.mark.asyncio
async def test_an_issueless_developer_that_reports_the_shared_name_cannot_claim_it() -> None:
    """The shared name may hold another job's work (any job from before #558).

    It exists on origin, so the push check would pass and the PR fallback
    would deliver that other job's commits under this job.
    """
    import embry0.workflows.issue_to_pr.nodes as nodes

    shared = _shared()

    async def _agent(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        output = f'Done.\n{{"pr_url": null, "branch": "{shared}", "summary": "s", "files_changed": []}}'
        return {
            "agent_outputs": [{"agent_type": "developer", "is_error": False, "output": output}],
            "events": [],
            "total_cost_usd": 0.01,
        }

    sessions = AsyncMock()
    sessions.get = AsyncMock(return_value=None)
    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=AsyncMock(side_effect=_agent)),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
        patch("embry0.workflows.issue_to_pr.nodes._verify_branch_pushed", new=AsyncMock(return_value=True)),
    ):
        command = await nodes.developer_node(
            _state(FIRST),
            {
                "configurable": {
                    "agent_runner": object(),
                    "credentials": {},
                    "agent_sessions_repo": sessions,
                    "docker": _Sandbox(_Origin({shared: "job-0123456789ab"})),
                }
            },
        )

    assert command.update["branch_name"] == f"{shared}-bc25a9c3", (
        f"an issue-less job must not record the shared name as its branch: {command.update['branch_name']!r}"
    )


@pytest.mark.asyncio
async def test_a_job_resumed_on_the_shared_name_it_started_on_keeps_it() -> None:
    """A job from before #558 has the shared name in its checkpoint; a resume must not move it."""
    shared = _shared()
    sandbox = _Sandbox(_Origin({shared: FIRST}))

    branch, taken = await _developer_working_branch(_state(FIRST, branch_name=shared), {"docker": sandbox})

    assert (branch, taken) == (shared, None), f"a resumed job must keep the branch it started on: {branch!r}"
    assert sandbox.commands == []


@pytest.mark.asyncio
async def test_the_unfinished_run_pause_names_the_issueless_jobs_own_branch() -> None:
    """The pause re-derives the branch when a checkpoint lacks it (#358); it must be the one the developer used."""
    import embry0.workflows.issue_to_pr.nodes as nodes

    captured: dict[str, Any] = {}

    def _interrupt(summary: dict[str, Any]) -> str:
        captured.update(summary)
        return "abandon"

    with (
        patch.object(nodes, "get_stream_writer", return_value=lambda _: None),
        patch.object(nodes, "interrupt", new=_interrupt),
    ):
        await nodes.max_retries_node(
            {**_state(FIRST), "current_stage": "dev_run_unfinished", "pr_url": ""}, config=None
        )

    own = f"{_shared()}-bc25a9c3"
    assert f"`{own}`" in captured["message"], f"the pause must name the job's own branch {own}: {captured['message']}"
