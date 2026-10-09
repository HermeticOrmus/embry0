"""git-proxy authenticates as the enrolled identity, and only to its host (#166, #521).

A sandbox works on exactly one repository, so its forge host and username are
known at enrollment, which lets one proxy serve both forges. The credential is
sent only to an upstream on that host, as git's credential protocol did when a
helper answered for one ``host``.
"""

import structlog

from embry0.execution.proxy.git_proxy import (
    DEFAULT_GIT_HOST,
    DEFAULT_GIT_USERNAME,
    create_git_proxy_app,
)

from .git_forge import ADMIN, ProxyRig, RecordingUpstream, basic

BEARER = "sandbox-bearer-token-long-enough-to-pass-01"


def _rig(host: str) -> ProxyRig:
    return ProxyRig(create_git_proxy_app("ghp-global", ADMIN), RecordingUpstream(), upstream_host=host)


async def test_defaults_to_github_when_no_identity_enrolled():
    """Every pre-#166 sandbox meant GitHub; that behaviour must be preserved."""
    assert DEFAULT_GIT_HOST == "github.com" and DEFAULT_GIT_USERNAME == "x-access-token"
    async with _rig("github.com") as rig:
        await rig.enroll("sbx-1", BEARER)
        assert await rig.credential_sent(BEARER) == basic("x-access-token", "ghp-global")


async def test_uses_the_enrolled_gitlab_identity():
    async with _rig("gitlab.com") as rig:
        await rig.enroll("sbx-1", BEARER, git_host="gitlab.com", git_username="oauth2", github_token="glpat-x")
        assert await rig.credential_sent(BEARER) == basic("oauth2", "glpat-x")


async def test_uses_a_self_managed_host():
    async with _rig("git.internal.example") as rig:
        await rig.enroll("sbx-1", BEARER, git_host="git.internal.example", github_token="glpat-x")
        # username defaults to oauth2 — GitLab's convention — when unspecified.
        assert await rig.credential_sent(BEARER) == basic("oauth2", "glpat-x")


async def test_re_enrollment_replaces_the_identity():
    """A reused sandbox id must not keep a stale forge."""
    async with _rig("github.com") as rig:
        await rig.enroll("sbx-1", BEARER, git_host="gitlab.com", github_token="glpat-x")
        await rig.enroll("sbx-1", BEARER)  # re-enrol with no identity
        assert await rig.credential_sent(BEARER) == basic("x-access-token", "ghp-global")


async def test_a_credential_is_not_sent_to_another_host():
    """A GitLab credential must never reach a GitHub upstream, and the reverse."""
    async with _rig("github.com") as rig:
        await rig.enroll("sbx-1", BEARER, git_host="gitlab.com", github_token="glpat-x")
        with structlog.testing.capture_logs() as logs:
            assert await rig.credential_sent(BEARER) == "", "the credential went to a host it was not enrolled for"
    mismatch = [e for e in logs if e["event"] == "git_proxy_credential_host_mismatch"]
    assert mismatch and mismatch[0]["credential_host"] == "gitlab.com" and mismatch[0]["upstream_host"] == "github.com"


async def test_unenrolled_request_reaches_nothing():
    async with _rig("github.com") as rig:
        assert await rig.credential_sent(BEARER) is None
