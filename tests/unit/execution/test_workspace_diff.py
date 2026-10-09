"""workspace_diff — base-branch resolution (RAV-1405) + changed-file diff."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from embry0.execution.workspace_diff import compute_changed_files_via_diff, resolve_base_branch


def _docker(run_output: str | Exception = "") -> Mock:
    docker = Mock()
    docker.build_exec_cmd = Mock(side_effect=lambda cid, cmd: ["exec", cid, *cmd])
    if isinstance(run_output, Exception):
        docker.run_cmd = AsyncMock(side_effect=run_output)
    else:
        docker.run_cmd = AsyncMock(return_value=run_output)
    return docker


@pytest.mark.asyncio
async def test_configured_base_wins_without_docker_call() -> None:
    docker = _docker("origin/master\n")
    assert await resolve_base_branch(docker=docker, container_id="c1", configured="release/1.2") == "release/1.2"
    docker.run_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolves_origin_head_symbolic_ref() -> None:
    # `git symbolic-ref --short refs/remotes/origin/HEAD` prints origin/<name>.
    docker = _docker("origin/master\n")
    assert await resolve_base_branch(docker=docker, container_id="c1") == "master"


@pytest.mark.asyncio
async def test_accepts_bare_branch_name() -> None:
    docker = _docker("develop\n")
    assert await resolve_base_branch(docker=docker, container_id="c1") == "develop"


@pytest.mark.asyncio
async def test_empty_output_falls_back_to_main() -> None:
    # origin/HEAD unset (non-git workspace, detached edge cases) → `|| true` → "".
    docker = _docker("")
    assert await resolve_base_branch(docker=docker, container_id="c1") == "main"


@pytest.mark.asyncio
async def test_exec_failure_falls_back_to_main() -> None:
    docker = _docker(RuntimeError("exec failed"))
    assert await resolve_base_branch(docker=docker, container_id="c1") == "main"


@pytest.mark.asyncio
async def test_disallowed_characters_fall_back_to_main() -> None:
    docker = _docker("bad branch; rm -rf /\n")
    assert await resolve_base_branch(docker=docker, container_id="c1") == "main"


@pytest.mark.asyncio
async def test_diff_returns_stripped_file_list() -> None:
    docker = Mock()
    docker.build_exec_cmd = Mock(side_effect=lambda cid, cmd: ["exec", cid, *cmd])
    docker.run_cmd = AsyncMock(side_effect=["", "a.py\n b/c.ts \n\n"])
    files = await compute_changed_files_via_diff(docker=docker, container_id="c1", base_branch="master")
    assert files == ["a.py", "b/c.ts"]
    # The fetch + diff both target the given base, not a hardcoded main.
    fetch_shell = docker.run_cmd.await_args_list[0].args[0][-1]
    diff_shell = docker.run_cmd.await_args_list[1].args[0][-1]
    assert "master" in fetch_shell and "main" not in fetch_shell
    # #398: the merge-base diff (three dots). Two dots is a tree comparison
    # that counts every commit main gained since the fork as the branch's own.
    assert "origin/master...HEAD" in diff_shell, diff_shell
    assert "origin/master..HEAD" not in diff_shell.replace("origin/master...HEAD", ""), (
        f"two-dot diff would attribute main's newer commits to the branch: {diff_shell}"
    )


@pytest.mark.asyncio
async def test_resolver_command_includes_ls_remote_fallback() -> None:
    """RAV-1400: single-branch clones (QA bootstrap: --branch <ref>) never set
    a local origin/HEAD — the resolver's shell must fall back to ls-remote."""
    docker = _docker("master\n")
    assert await resolve_base_branch(docker=docker, container_id="c1") == "master"
    shell = docker.run_cmd.await_args.args[0][-1]
    assert "git symbolic-ref --short refs/remotes/origin/HEAD" in shell
    assert "git ls-remote --symref origin HEAD" in shell


@pytest.mark.asyncio
async def test_resolver_takes_first_line_of_multiline_output() -> None:
    # ls-remote emits the symref line first when the local lookup was empty;
    # only the first parsed line counts.
    docker = _docker("master\nsomething-else\n")
    assert await resolve_base_branch(docker=docker, container_id="c1") == "master"
