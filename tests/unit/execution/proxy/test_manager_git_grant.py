"""ProxyManager gives the git-proxy a per-sandbox grant and refuses a proxy that ignores it (#521).

The grant is ``allowed_repos`` (the job's repository), ``git_upstream`` (where
the sandbox would clone from) and ``git_push``. A git-proxy that does not echo
it runs an image older than smart-HTTP forwarding: that image answers the
sandbox's credential helper with the forge token, so the sandbox is unenrolled
there and its creation fails.
"""

from __future__ import annotations

import base64
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from embry0.execution.proxy.manager import ProxyManager

_GRANT = ("allowed_repos", "git_upstream", "git_push", "git_push_branches")


def _manager(monkeypatch, *, echo=lambda body: {k: body[k] for k in _GRANT if k in body}):
    pm = ProxyManager(docker=AsyncMock(), proxy_admin_token="x" * 20)
    pm._http = object()  # bypass the "start() first" guard; the admin transport is faked below
    pm.git_proxy_url = "http://git-proxy:9101"
    pm.github_proxy_url = "http://github-proxy:9103"
    captured: list[dict] = []

    async def fake_exec(**kwargs):
        captured.append(kwargs)
        body = kwargs.get("body") or {}
        if kwargs["method"] == "DELETE":
            return {}
        if kwargs["container"] == "git-proxy":
            return {"status": "enrolled", **echo(body)}
        return {"status": "enrolled", "allowed_repos": body.get("allowed_repos", [])}

    monkeypatch.setattr(pm, "_exec_admin_request", fake_exec)
    return pm, captured


async def _enroll(pm: ProxyManager, sandbox_id: str, **kwargs):
    params = inspect.signature(pm.enroll_sandbox).parameters
    assert "git_upstream" in params and "git_push" in params, (
        "ProxyManager.enroll_sandbox takes no git grant: the git-proxy cannot be scoped to the job's repository"
    )
    return await pm.enroll_sandbox(sandbox_id, **kwargs)


def _git_body(captured: list[dict]) -> dict:
    return next(c["body"] for c in captured if c["container"] == "git-proxy" and c["method"] == "POST")


async def test_the_git_proxy_is_granted_the_jobs_repository_its_upstream_and_the_push_right(monkeypatch):
    pm, captured = _manager(monkeypatch)
    await _enroll(pm, "sb-1", forge="github", repo="Acme/Widgets", git_upstream="https://github.com/", git_push=True)
    body = _git_body(captured)
    assert body["allowed_repos"] == ["acme/widgets"]
    assert body["git_upstream"] == "https://github.com"
    assert body["git_push"] is True


async def test_push_is_off_unless_asked_for(monkeypatch):
    pm, captured = _manager(monkeypatch)
    await _enroll(pm, "sb-1", forge="github", repo="acme/widgets", git_upstream="https://github.com")
    assert _git_body(captured)["git_push"] is False


async def test_a_gitlab_project_path_is_granted_whole(monkeypatch):
    pm, captured = _manager(monkeypatch)
    await _enroll(
        pm,
        "sb-1",
        forge="gitlab",
        repo="Group/Sub/Proj",
        git_upstream="https://git.example.com:8443/gitlab",
        git_host="git.example.com",
    )
    body = _git_body(captured)
    assert body["allowed_repos"] == ["group/sub/proj"]
    assert body["git_upstream"] == "https://git.example.com:8443/gitlab"


@pytest.mark.parametrize(
    ("repo", "upstream", "event"),
    [
        (None, "https://github.com", None),
        ("acme/widgets", None, None),
        ("acme/widgets", "ftp://github.com", "git_proxy_upstream_invalid"),
        ("acme/../etc", "https://github.com", "git_proxy_scope_repo_invalid"),
    ],
)
async def test_without_a_usable_repo_and_upstream_the_grant_is_empty(monkeypatch, repo, upstream, event):
    pm, captured = _manager(monkeypatch)
    with structlog.testing.capture_logs() as logs:
        await _enroll(pm, "sb-1", forge="github", repo=repo, git_upstream=upstream, git_push=True)
    assert _git_body(captured)["allowed_repos"] == [], "a grant must name a repository and where it lives"
    if event:
        assert event in [e["event"] for e in logs]


async def test_a_git_proxy_that_does_not_echo_the_grant_is_revoked_and_the_sandbox_refused(monkeypatch):
    """The image before #521 ignores the grant and hands the forge token to the credential helper."""
    pm, captured = _manager(monkeypatch, echo=lambda body: {})
    with structlog.testing.capture_logs() as logs, pytest.raises(RuntimeError, match="did not confirm"):
        await _enroll(pm, "sb-1", forge="github", repo="acme/widgets", git_upstream="https://github.com")
    deletes = [c for c in captured if c["method"] == "DELETE"]
    assert [(c["container"], c["path"]) for c in deletes] == [("git-proxy", "/admin/enroll/sb-1")], (
        "the stale git-proxy must forget this sandbox's bearer before the sandbox is refused"
    )
    assert "git_proxy_grant_unconfirmed" in [e["event"] for e in logs]


@pytest.mark.parametrize(
    "wrong",
    [
        {"allowed_repos": ["acme/everything"], "git_upstream": "https://github.com", "git_push": False},
        {"allowed_repos": ["acme/widgets"], "git_upstream": "https://github.com", "git_push": True},
        {"allowed_repos": ["acme/widgets"], "git_upstream": "https://elsewhere.example", "git_push": False},
        {"allowed_repos": ["acme/widgets"], "git_upstream": "https://github.com", "git_push": "false"},
    ],
)
async def test_a_git_proxy_that_echoes_another_grant_is_revoked(monkeypatch, wrong):
    pm, captured = _manager(monkeypatch, echo=lambda body: wrong)
    with pytest.raises(RuntimeError, match="did not confirm"):
        await _enroll(pm, "sb-1", forge="github", repo="acme/widgets", git_upstream="https://github.com")
    assert [c["container"] for c in captured if c["method"] == "DELETE"] == ["git-proxy"]


@pytest.fixture
def _backend_dns(monkeypatch):
    monkeypatch.setattr("embry0.execution.proxy.manager.socket.gethostbyname", lambda host: "10.99.0.1")


def _launching_manager() -> ProxyManager:
    docker = MagicMock()
    docker.run_cmd = AsyncMock(return_value="")
    docker.build_rm_cmd = MagicMock(return_value=["docker", "rm", "x"])
    docker.build_network_cmd = MagicMock(return_value=["docker", "network", "x"])
    docker.build_run_proxy_cmd = MagicMock(return_value=(["docker", "run", "x"], "/dev/null"))
    return ProxyManager(docker, proxy_admin_token="admin-secret")


def _git_env(docker: MagicMock) -> dict[str, str]:
    for call in docker.build_run_proxy_cmd.call_args_list:
        if call.kwargs["name"] == "git-proxy":
            return dict(call.kwargs["env"])
    raise AssertionError("git-proxy was never launched")


@pytest.mark.usefixtures("_backend_dns")
async def test_the_git_proxy_reaches_the_forge_and_trusts_its_private_ca():
    """It forwards to the forge now, so it needs egress and the CA the gitlab-proxy gets."""
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    pm = _launching_manager()
    await pm.start(gitlab_token="glpat-x", gitlab_api_base="https://gl.example/api/v4", gitlab_ca_pem=pem)
    assert "git-proxy" in pm.internet_proxies(), "git-proxy cannot reach the forge without sandbox-internet"
    assert base64.b64decode(_git_env(pm._docker)["GITLAB_CA_PEM"]).decode() == pem
    await pm.stop()


@pytest.mark.usefixtures("_backend_dns")
async def test_no_ca_means_no_ca_env_on_the_git_proxy():
    pm = _launching_manager()
    await pm.start(github_token="ghp_x")
    assert "GITLAB_CA_PEM" not in _git_env(pm._docker)
    await pm.stop()


async def test_a_push_grant_names_its_branches_and_the_job(monkeypatch):
    """#559: the git-proxy refuses every ref update outside ``git_push_branches``."""
    pm, captured = _manager(monkeypatch)
    await _enroll(
        pm,
        "sb-1",
        forge="github",
        repo="acme/widgets",
        git_upstream="https://github.com",
        git_push=True,
        git_push_branches=["embry0/iss-1-x", "embry0/iss-1-x", "refs/heads/main", "a..b"],
        job_id="job-1",
    )
    body = _git_body(captured)
    assert body["git_push_branches"] == ["embry0/iss-1-x"], "a name the proxy would refuse is dropped, not sent"
    assert body["job_id"] == "job-1"


async def test_a_read_only_grant_sends_no_branches(monkeypatch):
    pm, captured = _manager(monkeypatch)
    await _enroll(pm, "sb-1", forge="github", repo="acme/widgets", git_upstream="https://github.com")
    assert "git_push_branches" not in _git_body(captured)


async def test_a_git_proxy_that_does_not_echo_the_push_branches_is_refused(monkeypatch):
    """The image before #559 ignores the branches and lets a push touch any ref: fail closed."""
    old_image = ("allowed_repos", "git_upstream", "git_push")
    pm, captured = _manager(monkeypatch, echo=lambda body: {k: body[k] for k in old_image if k in body})
    with pytest.raises(RuntimeError, match="did not confirm"):
        await _enroll(
            pm,
            "sb-1",
            forge="github",
            repo="acme/widgets",
            git_upstream="https://github.com",
            git_push=True,
            git_push_branches=["embry0/iss-1-x"],
        )
    assert [c["container"] for c in captured if c["method"] == "DELETE"] == ["git-proxy"]
    # A read-only sandbox does not depend on the new field, so it still works against that image.
    pm, _ = _manager(monkeypatch, echo=lambda body: {k: body[k] for k in old_image if k in body})
    await _enroll(pm, "sb-2", forge="github", repo="acme/widgets", git_upstream="https://github.com")


async def test_set_git_push_branches_replaces_the_branches_and_checks_the_echo(monkeypatch):
    pm, captured = _manager(monkeypatch)
    await pm.set_git_push_branches("sb-1", ["embry0/iss-1-x"])
    put = [c for c in captured if c["method"] == "PUT"]
    assert [(c["container"], c["path"], c["body"]) for c in put] == [
        ("git-proxy", "/admin/git-push-branches/sb-1", {"git_push_branches": ["embry0/iss-1-x"]})
    ]

    pm, _ = _manager(monkeypatch, echo=lambda body: {})
    with pytest.raises(RuntimeError, match="did not confirm"):
        await pm.set_git_push_branches("sb-1", ["embry0/iss-1-x"])
    with pytest.raises(RuntimeError, match="not branches"):
        await pm.set_git_push_branches("sb-1", ["refs/heads/main"])
