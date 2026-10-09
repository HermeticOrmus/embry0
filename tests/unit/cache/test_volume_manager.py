from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.cache.volume_manager import (
    SharedVolumeManager,
    VolumeScope,
)
from embry0.execution.runtime import DockerRuntime


def _stub_docker():
    docker = MagicMock()
    docker._build_base_cmd = lambda: ["docker"]
    docker.run_cmd = AsyncMock(return_value="")
    return docker


@pytest.mark.asyncio
async def test_create_per_job_volume_uses_job_id_in_name():
    docker = _stub_docker()
    state_repo = AsyncMock()
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=state_repo)
    name = await mgr.ensure(scope=VolumeScope.PER_JOB, scope_key="job-1")
    assert "job-1" in name
    state_repo.upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_per_repo_uses_repo_in_name():
    docker = _stub_docker()
    state_repo = AsyncMock()
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=state_repo)
    name = await mgr.ensure(scope=VolumeScope.PER_REPO, scope_key="org/r1")
    assert "org" in name and "r1" in name


@pytest.mark.asyncio
async def test_destroy_removes_volume():
    docker = _stub_docker()
    state_repo = AsyncMock()
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=state_repo)
    await mgr.destroy("embry0-qa-vol-job-1")
    cmds = [" ".join(c.args[0]) for c in docker.run_cmd.call_args_list]
    assert any("volume rm" in s for s in cmds)


@pytest.mark.asyncio
async def test_create_skips_when_volume_already_exists():
    """Ensure is idempotent: re-calling for same scope_key returns same name."""
    docker = _stub_docker()
    docker.run_cmd = AsyncMock(
        side_effect=[
            "embry0-qa-vol-job-1\n",  # `volume ls` returns existing
        ]
    )
    state_repo = AsyncMock()
    state_repo.get = AsyncMock(return_value=None)
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=state_repo)
    name = await mgr.ensure(scope=VolumeScope.PER_JOB, scope_key="job-1")
    assert name == "embry0-qa-vol-job-1"
    # `volume create` was NOT called
    cmds = [" ".join(c.args[0]) for c in docker.run_cmd.call_args_list]
    assert not any("volume create" in s for s in cmds)


def _cmds(docker) -> list[str]:
    return [" ".join(c.args[0]) for c in docker.run_cmd.call_args_list]


@pytest.mark.asyncio
async def test_create_stamps_ownership_labels():
    """#274: an unlabelled volume is invisible to the reaper, so it leaks."""
    docker = _stub_docker()
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=AsyncMock(), instance_id="a1b2c3d4e5f60718")
    await mgr.ensure(scope=VolumeScope.PER_JOB, scope_key="job-1")
    create = next(s for s in _cmds(docker) if "volume create" in s)
    assert "--label embry0.managed=true" in create
    assert "--label embry0.instance=a1b2c3d4e5f60718" in create
    assert "--label embry0.volume_scope=per-job" in create
    assert "--label embry0.job_id=job-1" in create
    assert create.endswith("embry0-qa-vol-job-1")


@pytest.mark.asyncio
async def test_ensure_creates_a_volume_whose_name_only_prefixes_an_existing_one():
    """Docker's name filter is a substring match; ensure() must compare exactly (#328).

    Skipping the create here lets Docker auto-create the volume, unlabelled,
    on first mount, and the reaper never sees it.
    """
    docker = _stub_docker()

    async def run_cmd(cmd, timeout=None):
        if cmd[1:3] == ["volume", "ls"]:
            return "embry0-qa-vol-org-r10\n"  # what `--filter name=embry0-qa-vol-org-r1` matches
        return ""

    docker.run_cmd = AsyncMock(side_effect=run_cmd)
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=AsyncMock())
    name = await mgr.ensure(scope=VolumeScope.PER_REPO, scope_key="org/r1")
    creates = [c for c in _cmds(docker) if c.startswith("docker volume create")]
    assert name == "embry0-qa-vol-org-r1"
    assert len(creates) == 1 and creates[0].endswith(" embry0-qa-vol-org-r1"), (
        f"ensure() took embry0-qa-vol-org-r10 for embry0-qa-vol-org-r1 and skipped the labelled create: {_cmds(docker)}"
    )


@pytest.mark.asyncio
async def test_destroy_for_job_removes_named_and_labelled_volumes():
    docker = _stub_docker()

    async def run_cmd(cmd, timeout=None):
        if cmd[1:3] == ["volume", "ls"]:
            if "name=embry0-qa-vol-job-1" in cmd:
                # Docker's name filter is a substring match.
                return "embry0-qa-vol-job-1\nembry0-qa-vol-job-10\n"
            return "embry0-qa-vol-job-1\nextra-vol\n"
        return ""

    docker.run_cmd = AsyncMock(side_effect=run_cmd)
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=AsyncMock())
    removed = await mgr.destroy_for_job("job-1")
    cmds = _cmds(docker)
    assert "docker volume ls --quiet --filter label=embry0.job_id=job-1" in cmds
    assert "docker volume rm embry0-qa-vol-job-1" in cmds
    assert "docker volume rm extra-vol" in cmds
    assert "docker volume rm embry0-qa-vol-job-10" not in cmds
    assert removed == ["embry0-qa-vol-job-1", "extra-vol"]


@pytest.mark.asyncio
async def test_destroy_for_job_first_removes_containers_holding_the_volume():
    """A cancelled fan-out leaves sub-task containers mounting the volume."""
    docker = _stub_docker()

    async def run_cmd(cmd, timeout=None):
        if cmd[1] == "ps":
            return "c1\tqa-sub-1\tcreated\tExited (1)\t1 hour ago\nc2\tqa-sub-2\tcreated\tUp\t1 hour ago\n"
        if cmd[1:3] == ["volume", "ls"]:
            return "embry0-qa-vol-job-1\n"
        return ""

    docker.run_cmd = AsyncMock(side_effect=run_cmd)
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=AsyncMock())
    await mgr.destroy_for_job("job-1")
    cmds = _cmds(docker)
    ps = next(i for i, c in enumerate(cmds) if c.startswith("docker ps -a --filter volume=embry0-qa-vol-job-1 "))
    rm_c = cmds.index("docker rm -f c1 c2")
    rm_v = cmds.index("docker volume rm embry0-qa-vol-job-1")
    assert ps < rm_c < rm_v


@pytest.mark.asyncio
async def test_destroy_for_job_never_raises():
    docker = _stub_docker()
    docker.run_cmd = AsyncMock(side_effect=RuntimeError("daemon down"))
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=AsyncMock())
    assert await mgr.destroy_for_job("job-1") == []


@pytest.mark.asyncio
async def test_destroy_for_job_without_a_volume_issues_no_removal():
    """Most jobs never create a volume — that must be a silent no-op."""
    docker = _stub_docker()
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=AsyncMock())
    assert await mgr.destroy_for_job("job-1") == []
    assert not any(" rm" in s for s in _cmds(docker))


@pytest.mark.asyncio
async def test_destroy_for_job_in_use_volume_is_not_reported_as_removed():
    docker = _stub_docker()

    async def run_cmd(cmd, timeout=None):
        if cmd[1:3] == ["volume", "ls"]:
            return "embry0-qa-vol-job-1\n"
        if cmd[1:3] == ["volume", "rm"]:
            raise RuntimeError("volume is in use")
        return ""

    docker.run_cmd = AsyncMock(side_effect=run_cmd)
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=AsyncMock())
    assert await mgr.destroy_for_job("job-1") == []


@pytest.mark.asyncio
async def test_create_without_an_instance_id_stamps_no_instance_label():
    docker = _stub_docker()
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=AsyncMock())
    await mgr.ensure(scope=VolumeScope.PER_JOB, scope_key="job-1")
    create = next(s for s in _cmds(docker) if "volume create" in s)
    assert "embry0.instance" not in create


@pytest.mark.asyncio
async def test_destroy_for_job_label_lookup_is_scoped_to_this_instance():
    docker = _stub_docker()
    mgr = SharedVolumeManager(runtime=DockerRuntime(docker), state_repo=AsyncMock(), instance_id="a1b2c3d4e5f60718")
    await mgr.destroy_for_job("job-1")
    assert (
        "docker volume ls --quiet --filter label=embry0.job_id=job-1 --filter label=embry0.instance=a1b2c3d4e5f60718"
        in _cmds(docker)
    )
