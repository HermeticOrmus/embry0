"""One forge lookup for every sandbox call site (spec 3.6).

`issue_to_pr/nodes/_init.py` grew this lookup inline; QA, onboard and the
cache layers never did it at all and cloned github.com for every repo, which
is why a GitLab job's bootstrap clone silently 404'd. The helper keeps the
lookup, its logging and its failure mode in one place: an unregistered repo
on a deployment with no DEFAULT_FORGE raises UnregisteredRepoError so the
caller fails the job with ERR_REPO_UNREGISTERED instead of cloning the wrong
host.
"""

from __future__ import annotations

from typing import Any

import structlog

from embry0.storage.repositories.repos import DEFAULT_FORGE, UnregisteredRepoError

logger = structlog.get_logger(__name__)


async def resolve_forge(repos_repo: Any, repo: str, *, job_id: str = "") -> str:
    """Forge for ``repo`` — "github" or "gitlab".

    ``repos_repo`` may be None and ``repo`` may be empty (unit-test graphs,
    context-less jobs, legacy callers that never wired the registry); both
    keep the pre-#162 behaviour of GitHub, there being no configured default
    to honour. A lookup that fails for an infrastructure reason (DB down)
    degrades to the registry's *own* configured default rather than killing a
    job that would have worked before — but only when that default is set. A
    deployment that set it empty to refuse guessing gets the infra error
    raised, not a github.com clone it explicitly ruled out; an unregistered
    repo on such a deployment likewise propagates its UnregisteredRepoError.
    """
    if repos_repo is None or not repo:
        return DEFAULT_FORGE
    try:
        forge = str(await repos_repo.forge_for(repo))
    except UnregisteredRepoError:
        logger.warning("forge_unregistered_repo", job_id=job_id, repo=repo)
        raise
    except Exception:
        # The registry's own default, not the module constant: a tenant that
        # set DEFAULT_FORGE="" must not have a transient DB error turned into
        # a github.com clone, where a two-segment repo path can even collide
        # with an unrelated public repo (Spec 1A §3.8). getattr keeps
        # duck-typed fakes and legacy callers working.
        fallback = str(getattr(repos_repo, "default_forge", DEFAULT_FORGE))
        if not fallback:
            logger.warning("forge_lookup_failed_no_default", job_id=job_id, repo=repo, exc_info=True)
            raise
        logger.warning("forge_lookup_failed", job_id=job_id, repo=repo, forge=fallback, exc_info=True)
        return fallback
    logger.info("forge_resolved", job_id=job_id, repo=repo, forge=forge)
    return forge
