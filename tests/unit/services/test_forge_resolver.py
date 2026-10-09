"""resolve_forge: one lookup, one log line, one failure mode (spec 3.6)."""

import pytest

from embry0.services.forge_resolver import resolve_forge
from embry0.storage.repositories.repos import UnregisteredRepoError


class _Repos:
    def __init__(self, forge="github", raises=None):
        self._forge = forge
        self._raises = raises
        self.seen = []

    async def forge_for(self, repo):
        self.seen.append(repo)
        if self._raises is not None:
            raise self._raises
        return self._forge


async def test_returns_the_registered_forge():
    repos = _Repos("gitlab")
    assert await resolve_forge(repos, "acme/group/backend") == "gitlab"
    assert repos.seen == ["acme/group/backend"]


async def test_returns_github_when_that_is_what_the_registry_answers():
    """Covers both registry answers that say "github": a repo registered on
    GitHub, and an unregistered repo on a deployment whose DEFAULT_FORGE is
    still "github" — ``forge_for`` returns the default itself, so the
    resolver cannot and need not tell the two apart."""
    repos = _Repos("github")
    assert await resolve_forge(repos, "org/repo") == "github"
    assert repos.seen == ["org/repo"]


async def test_missing_registry_keeps_the_pre_162_github_default():
    assert await resolve_forge(None, "org/repo") == "github"


async def test_blank_repo_never_hits_the_registry():
    repos = _Repos("gitlab")
    assert await resolve_forge(repos, "") == "github"
    assert repos.seen == []


async def test_unregistered_repo_propagates_so_the_caller_can_fail_the_job():
    repos = _Repos(raises=UnregisteredRepoError("org/ghost is not registered"))
    with pytest.raises(UnregisteredRepoError):
        await resolve_forge(repos, "org/ghost", job_id="job-1")


async def test_infrastructure_failure_degrades_to_the_default():
    """A DB blip must not kill a job that would have cloned github.com anyway."""
    repos = _Repos(raises=RuntimeError("db down"))
    assert await resolve_forge(repos, "org/repo", job_id="job-1") == "github"


class _ReposWithDefault(_Repos):
    """A registry that exposes its configured default, as ReposRepository does."""

    def __init__(self, default_forge, **kwargs):
        super().__init__(**kwargs)
        self.default_forge = default_forge


async def test_infrastructure_failure_uses_the_registrys_own_default():
    repos = _ReposWithDefault("github", raises=RuntimeError("db down"))
    assert await resolve_forge(repos, "org/repo", job_id="job-1") == "github"


async def test_infrastructure_failure_raises_when_the_deployment_refuses_a_default():
    """DEFAULT_FORGE="" means "never assume github.com" — a DB blip must fail
    the job, not retarget the clone at an unrelated public repo (spec 3.8)."""
    repos = _ReposWithDefault("", raises=RuntimeError("db down"))
    with pytest.raises(RuntimeError, match="db down"):
        await resolve_forge(repos, "org/repo", job_id="job-1")
