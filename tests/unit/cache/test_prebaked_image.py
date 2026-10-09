from datetime import UTC
from unittest.mock import AsyncMock

import pytest

from embry0.cache.prebaked_image import PrebakedImageLookup
from embry0.execution.runtime import DockerRuntime
from embry0.storage.repositories.qa_image_tags import QAImageTagRow


def _row(image_tag: str, lockfile_sha: str = "deadbeef") -> QAImageTagRow:
    from datetime import datetime

    return QAImageTagRow(
        repo="org/r1",
        image_tag=image_tag,
        lockfile_sha=lockfile_sha,
        built_at=datetime(2026, 5, 5, tzinfo=UTC),
        built_by="cli",
        is_active=True,
        metadata={},
    )


@pytest.mark.asyncio
async def test_lookup_returns_tag_when_active_exists():
    image_repo = AsyncMock()
    image_repo.get_active = AsyncMock(return_value=_row("repo:t1"))
    lookup = PrebakedImageLookup(image_repo=image_repo)
    tag = await lookup.get_tag_for_repo("org/r1")
    assert tag == "repo:t1"


@pytest.mark.asyncio
async def test_lookup_returns_none_when_no_active_tag():
    image_repo = AsyncMock()
    image_repo.get_active = AsyncMock(return_value=None)
    lookup = PrebakedImageLookup(image_repo=image_repo)
    tag = await lookup.get_tag_for_repo("org/r1")
    assert tag is None


@pytest.mark.asyncio
async def test_lookup_skips_when_image_repo_is_none():
    """When the cache state DB isn't configured (e.g. unit-test environment),
    PrebakedImageLookup gracefully reports None — sub-task falls back to base."""
    lookup = PrebakedImageLookup(image_repo=None)
    tag = await lookup.get_tag_for_repo("org/r1")
    assert tag is None


# --- lookup_prebaked_tag: the one path graph nodes use (#334) ---------------


@pytest.mark.asyncio
async def test_node_lookup_skips_an_image_whose_base_was_rebuilt():
    from unittest.mock import MagicMock

    from embry0.cache import prebaked_staleness
    from embry0.cache.prebaked_image import lookup_prebaked_tag

    prebaked_staleness.reset_warning_memory()
    from dataclasses import replace

    base = "registry:5000/embry0-sandbox-qa:latest"
    row = replace(_row("repo:t1"), base_image=base, base_image_digest="sha256:" + "1" * 64)
    image_repo = AsyncMock()
    image_repo.get_active = AsyncMock(return_value=row)

    def _docker(registry_digest: str) -> MagicMock:
        docker = MagicMock()
        docker._build_base_cmd = lambda: ["docker"]

        async def run_cmd(cmd, timeout=60):
            # pull echoes the ref; inspect prints "<repo digests>|<local id>"
            if "pull" in cmd:
                return cmd[-1]
            return f"registry:5000/embry0-sandbox-qa@{registry_digest}|sha256:{'c' * 64}"

        docker.run_cmd = run_cmd
        return docker

    # The registry moved on: the image is a commit of a base that no longer is.
    stale = {"qa_image_tags_repo": image_repo, "runtime": DockerRuntime(_docker("sha256:" + "2" * 64))}
    assert await lookup_prebaked_tag(stale, "org/r1") is None

    fresh = {"qa_image_tags_repo": image_repo, "runtime": DockerRuntime(_docker("sha256:" + "1" * 64))}
    assert await lookup_prebaked_tag(fresh, "org/r1") == "repo:t1"


@pytest.mark.asyncio
async def test_node_lookup_reuses_the_resolver_the_graph_was_given():
    """#338 review: a resolver built per call never gets a cache hit. The
    orchestrator threads one through, and both selection sites share it."""
    from unittest.mock import MagicMock

    from embry0.cache.prebaked_image import lookup_prebaked_tag

    image_repo = AsyncMock()
    image_repo.get_active = AsyncMock(return_value=_row("repo:t1"))
    resolver = MagicMock(current=AsyncMock(return_value="sha256:" + "1" * 64))
    docker = MagicMock()
    docker.run_cmd = AsyncMock(side_effect=AssertionError("must not talk to docker itself"))
    configurable = {
        "qa_image_tags_repo": image_repo,
        "runtime": DockerRuntime(docker),
        "qa_base_digest_resolver": resolver,
    }

    assert await lookup_prebaked_tag(configurable, "org/r1") == "repo:t1"


@pytest.mark.asyncio
async def test_node_lookup_without_repo_or_runtime_degrades():
    from embry0.cache.prebaked_image import lookup_prebaked_tag

    assert await lookup_prebaked_tag({}, "org/r1") is None
    image_repo = AsyncMock()
    image_repo.get_active = AsyncMock(return_value=_row("repo:t1"))
    assert await lookup_prebaked_tag({"qa_image_tags_repo": image_repo}, "org/r1") == "repo:t1"
