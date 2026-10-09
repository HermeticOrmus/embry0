"""Unit tests for embry0/cache/image_builder_cli.py — Task B4."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from embry0.cache.image_builder import ImageBuildResult
from embry0.cache.image_builder_cli import run_build_qa_image
from embry0.execution.runtime import DockerRuntime


@pytest.mark.asyncio
async def test_run_skips_when_existing_active_tag_matches_lockfile(monkeypatch):
    """If qa_image_tags already has an active row for (repo, current lockfile_sha),
    no new build kicks off (idempotency)."""
    from embry0.storage.repositories.qa_image_tags import QAImageTagRow

    image_repo = AsyncMock()
    image_repo.get_active = AsyncMock(
        return_value=QAImageTagRow(
            repo="org/r1",
            image_tag="org_r1:existing",
            lockfile_sha="abcdef" + "0" * 58,
            built_at=datetime(2026, 5, 5, tzinfo=UTC),
            built_by="cli",
            is_active=True,
            metadata={},
        )
    )

    # Stub: the current lockfile sha "as-cloned" matches the existing active tag.
    async def fake_compute_remote_lockfile_sha(*_a, **_kw):
        return "abcdef" + "0" * 58

    monkeypatch.setattr(
        "embry0.cache.image_builder_cli._compute_remote_lockfile_sha",
        fake_compute_remote_lockfile_sha,
    )

    # If the build path were called, this AsyncMock would record it.
    build_mock = AsyncMock()
    monkeypatch.setattr("embry0.cache.image_builder_cli.build_qa_image", build_mock)

    result = await run_build_qa_image(
        repo="org/r1",
        branch="main",
        force=False,
        deps={
            "image_repo": image_repo,
            "sandbox_mgr": AsyncMock(),
            "runtime": AsyncMock(),
            "profiles_repo": AsyncMock(),
            "proxy_mgr": AsyncMock(),
        },
    )
    assert result == "skipped"
    build_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_force_rebuilds_even_with_matching_sha(monkeypatch):
    """--force flag triggers a fresh build regardless of existing active tag."""
    from embry0.storage.repositories.qa_image_tags import QAImageTagRow

    image_repo = AsyncMock()
    image_repo.get_active = AsyncMock(
        return_value=QAImageTagRow(
            repo="org/r1",
            image_tag="org_r1:existing",
            lockfile_sha="aaa",
            built_at=datetime(2026, 5, 5, tzinfo=UTC),
            built_by="cli",
            is_active=True,
            metadata={},
        )
    )

    async def fake_compute_remote_lockfile_sha(*_a, **_kw):
        return "aaa"

    monkeypatch.setattr(
        "embry0.cache.image_builder_cli._compute_remote_lockfile_sha",
        fake_compute_remote_lockfile_sha,
    )

    build_mock = AsyncMock(return_value=ImageBuildResult(image_tag="t2", lockfile_sha="aaa", head_sha="x"))
    monkeypatch.setattr("embry0.cache.image_builder_cli.build_qa_image", build_mock)

    result = await run_build_qa_image(
        repo="org/r1",
        branch="main",
        force=True,
        deps={
            "image_repo": image_repo,
            "sandbox_mgr": AsyncMock(),
            "runtime": AsyncMock(),
            "profiles_repo": AsyncMock(),
            "proxy_mgr": AsyncMock(),
        },
    )
    assert result == "built"
    build_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_builds_when_no_active_tag_exists(monkeypatch):
    """When no active image tag exists for the repo, a new build is triggered."""
    image_repo = AsyncMock()
    image_repo.get_active = AsyncMock(return_value=None)

    async def fake_compute_remote_lockfile_sha(*_a, **_kw):
        return "abc"

    monkeypatch.setattr(
        "embry0.cache.image_builder_cli._compute_remote_lockfile_sha",
        fake_compute_remote_lockfile_sha,
    )

    build_mock = AsyncMock(return_value=ImageBuildResult(image_tag="t1", lockfile_sha="abc", head_sha="x"))
    monkeypatch.setattr("embry0.cache.image_builder_cli.build_qa_image", build_mock)

    result = await run_build_qa_image(
        repo="org/r1",
        branch="main",
        force=False,
        deps={
            "image_repo": image_repo,
            "sandbox_mgr": AsyncMock(),
            "runtime": AsyncMock(),
            "profiles_repo": AsyncMock(),
            "proxy_mgr": AsyncMock(),
        },
    )
    assert result == "built"


@pytest.mark.asyncio
async def test_unchanged_lockfile_still_rebuilds_when_the_base_image_moved(monkeypatch):
    """#334: "lockfile unchanged" is not "image still good" — an image committed
    from a since-rebuilt sandbox base carries the old runner."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from embry0.storage.repositories.qa_image_tags import QAImageTagRow

    sha = "abcdef" + "0" * 58
    image_repo = AsyncMock()
    image_repo.get_active = AsyncMock(
        return_value=QAImageTagRow(
            repo="org/r1",
            image_tag="org_r1:old",
            lockfile_sha=sha,
            built_at=datetime(2026, 8, 12, tzinfo=UTC),
            built_by="cli",
            is_active=True,
            metadata={},
            base_image="registry:5000/embry0-sandbox-qa:latest",
            base_image_digest="sha256:" + "1" * 64,
        )
    )

    async def same_sha(*_a, **_kw):
        return sha

    monkeypatch.setattr("embry0.cache.image_builder_cli._compute_remote_lockfile_sha", same_sha)
    build_mock = AsyncMock()
    monkeypatch.setattr("embry0.cache.image_builder_cli.build_qa_image", build_mock)

    base_repo = "registry:5000/embry0-sandbox-qa"

    def _docker(registry_digest: str) -> MagicMock:
        """pull echoes the ref; inspect prints "<repo digests>|<local id>"."""
        docker = MagicMock()
        docker._build_base_cmd = lambda: ["docker"]

        async def run_cmd(cmd, timeout=60):
            if "pull" in cmd:
                return cmd[-1]
            return f"{base_repo}@{registry_digest},|sha256:{'c' * 64}"

        docker.run_cmd = run_cmd
        return docker

    docker = _docker("sha256:" + "2" * 64)
    deps = {
        "image_repo": image_repo,
        "sandbox_mgr": AsyncMock(),
        "runtime": DockerRuntime(docker),
        "profiles_repo": AsyncMock(),
        "proxy_mgr": AsyncMock(),
    }

    assert await run_build_qa_image(repo="org/r1", branch="main", force=False, deps=deps) == "built"
    build_mock.assert_awaited_once()

    # Same base → the idempotent skip still holds.
    build_mock.reset_mock()
    deps["runtime"] = DockerRuntime(_docker("sha256:" + "1" * 64))
    assert await run_build_qa_image(repo="org/r1", branch="main", force=False, deps=deps) == "skipped"
    build_mock.assert_not_awaited()
