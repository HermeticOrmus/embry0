"""POST /api/v1/qa/images/build (EMB-42) — endpoint wiring tests.

The build core (`run_build_qa_image`) is unit-tested in
tests/unit/cli/test_build_qa_image_cli.py; here we verify the HTTP wiring:
dependency presence check, payload validation, outcome passthrough, and
ImageBuildError mapping.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


def _wire_fake_deps(app) -> None:
    app.state.sandbox_manager = MagicMock()
    app.state.runtime = MagicMock()
    app.state.profiles_repo = MagicMock()
    app.state.proxy_manager = MagicMock()
    app.state.qa_image_tags_repo = MagicMock()


async def test_build_returns_outcome(api_client):
    _wire_fake_deps(api_client.app)
    with patch(
        "embry0.cache.image_builder_cli.run_build_qa_image",
        new=AsyncMock(return_value="built"),
    ) as run:
        r = await api_client.post(
            "/api/v1/qa/images/build",
            json={"repo": "org/repo", "branch": "main", "force": False},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "built"
    kwargs = run.await_args.kwargs
    assert kwargs["repo"] == "org/repo"
    assert kwargs["deps"]["image_repo"] is api_client.app.state.qa_image_tags_repo


async def test_build_503_when_deps_missing(api_client):
    api_client.app.state.sandbox_manager = None
    api_client.app.state.runtime = None
    r = await api_client.post(
        "/api/v1/qa/images/build",
        json={"repo": "org/repo"},
    )
    assert r.status_code == 503
    assert "not initialized" in r.json()["detail"]


async def test_build_502_on_image_build_error(api_client):
    from embry0.cache.image_builder import ImageBuildError

    _wire_fake_deps(api_client.app)
    with patch(
        "embry0.cache.image_builder_cli.run_build_qa_image",
        new=AsyncMock(side_effect=ImageBuildError("npm ci exploded")),
    ):
        r = await api_client.post(
            "/api/v1/qa/images/build",
            json={"repo": "org/repo", "branch": "dev", "force": True},
        )
    assert r.status_code == 502
    assert "npm ci exploded" in r.json()["detail"]


async def test_build_rejects_malformed_repo(api_client):
    r = await api_client.post(
        "/api/v1/qa/images/build",
        json={"repo": "not-a-repo"},
    )
    assert r.status_code == 422


async def test_build_502_on_unexpected_build_exception(api_client):
    """Docker-layer faults raise plain RuntimeError — still a 502 with the
    message surfaced, never a raw 500."""
    _wire_fake_deps(api_client.app)
    with patch(
        "embry0.cache.image_builder_cli.run_build_qa_image",
        new=AsyncMock(side_effect=RuntimeError("Docker command failed: clone")),
    ):
        r = await api_client.post(
            "/api/v1/qa/images/build",
            json={"repo": "org/repo"},
        )
    assert r.status_code == 502
    assert "Docker command failed" in r.json()["detail"]


async def test_build_accepts_a_nested_gitlab_path(api_client):
    """Spec 3.5 / C5: this door is forge-aware, so a self-managed GitLab
    subgroup path must reach the builder instead of 422ing."""
    _wire_fake_deps(api_client.app)
    nested = "acme/group/backend"
    with patch(
        "embry0.cache.image_builder_cli.run_build_qa_image",
        new=AsyncMock(return_value="built"),
    ) as run:
        r = await api_client.post(
            "/api/v1/qa/images/build",
            json={"repo": nested},
        )
    assert r.status_code == 200
    assert run.await_args.kwargs["repo"] == nested


@pytest.mark.parametrize("repo", ["../evil", "org/../evil", "justname"])
async def test_build_rejects_traversal_and_single_segment(api_client, repo):
    """The old inline `[^/\\s]+/[^/\\s]+` accepted `..` as a segment and capped
    at two; the shared rule rejects both, and the repo reaches clone URLs."""
    _wire_fake_deps(api_client.app)
    r = await api_client.post("/api/v1/qa/images/build", json={"repo": repo})
    assert r.status_code == 422


async def test_build_accepts_a_gitlab_subgroup_and_forwards_its_forge(api_client):
    _wire_fake_deps(api_client.app)
    nested = "acme/group/backend"
    await api_client.post("/api/v1/repos", json={"repo": nested, "forge": "gitlab"})
    with patch(
        "embry0.cache.image_builder_cli.run_build_qa_image",
        new=AsyncMock(return_value="built"),
    ) as run:
        r = await api_client.post("/api/v1/qa/images/build", json={"repo": nested, "branch": "dev"})
    assert r.status_code == 200, r.text
    assert run.await_args.kwargs["forge"] == "gitlab"
    assert run.await_args.kwargs["gitlab_url"] == api_client.app.state.config.gitlab_url


async def test_build_defaults_to_github_for_an_unregistered_repo(api_client):
    """No `repos` row and a deployment that still assumes github → the bake
    behaves exactly as it did before #162."""
    _wire_fake_deps(api_client.app)
    with patch(
        "embry0.cache.image_builder_cli.run_build_qa_image",
        new=AsyncMock(return_value="built"),
    ) as run:
        r = await api_client.post("/api/v1/qa/images/build", json={"repo": "org/repo"})
    assert r.status_code == 200, r.text
    assert run.await_args.kwargs["forge"] == "github"


async def test_build_refuses_an_unregistered_repo_when_no_forge_can_be_assumed(api_client):
    """DEFAULT_FORGE="" → ERR_REPO_UNREGISTERED, never a github.com bake."""
    from embry0.storage.repositories.repos import UnregisteredRepoError

    _wire_fake_deps(api_client.app)
    repos_repo = MagicMock()
    repos_repo.forge_for = AsyncMock(side_effect=UnregisteredRepoError("org/repo has no repos row"))
    api_client.app.state.repos_repo = repos_repo
    with patch(
        "embry0.cache.image_builder_cli.run_build_qa_image",
        new=AsyncMock(return_value="built"),
    ) as run:
        r = await api_client.post("/api/v1/qa/images/build", json={"repo": "org/repo"})
    assert r.status_code == 422, r.text
    assert "ERR_REPO_UNREGISTERED" in r.json()["detail"]
    run.assert_not_awaited()
