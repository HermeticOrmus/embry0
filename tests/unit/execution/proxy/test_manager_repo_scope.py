"""ProxyManager scopes the forge API proxies to the job's repository (#520).

The proxy of the job's forge is enrolled with ``allowed_repos: [<repo>]``, the
other forge's proxy with ``[]``. A proxy that does not echo the scope runs an
image older than scoping and would forward any call with the credential, so the
sandbox is unenrolled from it again.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest
import structlog

from embry0.execution.proxy.manager import ProxyManager

_SCOPED = ("github-proxy", "gitlab-proxy")
_GIT_GRANT = ("allowed_repos", "git_upstream", "git_push")


def _git_proxy_echo(body: dict) -> dict:
    """A current git-proxy echoes its grant (#521); these tests are about the API proxies."""
    return {"status": "enrolled", **{k: body[k] for k in _GIT_GRANT if k in body}}


def _manager(monkeypatch, *, echo: bool = True, fail_delete: bool = False):
    pm = ProxyManager(docker=AsyncMock(), proxy_admin_token="x" * 20)
    pm._http = object()  # bypass the "start() first" guard; the admin transport is faked below
    pm.git_proxy_url = "http://git-proxy:9101"
    pm.github_proxy_url = "http://github-proxy:9103"
    pm.gitlab_proxy_url = "http://gitlab-proxy:9105"
    pm.xai_proxy_url = "http://xai-proxy:9106"
    captured: list[dict] = []

    async def fake_exec(**kwargs):
        captured.append(kwargs)
        if kwargs["method"] == "DELETE" and fail_delete:
            raise RuntimeError(f"enroll {kwargs['container']} {kwargs['path']} returned HTTP 503")
        body = kwargs.get("body") or {}
        if kwargs["container"] == "git-proxy":
            return _git_proxy_echo(body)
        if echo and "allowed_repos" in body:
            return {"status": "enrolled", "allowed_repos": body["allowed_repos"]}
        return {"status": "enrolled"}

    monkeypatch.setattr(pm, "_exec_admin_request", fake_exec)
    return pm, captured


async def _enroll(pm: ProxyManager, sandbox_id: str, **kwargs) -> str:
    assert "repo" in inspect.signature(pm.enroll_sandbox).parameters, (
        "ProxyManager.enroll_sandbox takes no repo: no enrollment can be scoped to the job's repository"
    )
    return await pm.enroll_sandbox(sandbox_id, **kwargs)


def _posts(captured: list[dict]) -> dict[str, dict]:
    return {c["container"]: c["body"] for c in captured if c["method"] == "POST"}


def _deletes(captured: list[dict]) -> list[str]:
    return [c["container"] for c in captured if c["method"] == "DELETE"]


async def test_a_github_job_scopes_github_proxy_to_its_repo(monkeypatch):
    pm, captured = _manager(monkeypatch)
    await _enroll(pm, "sb-1", github_token="ghp_SECRET", forge="github", repo="Acme/Widgets")
    bodies = _posts(captured)
    assert bodies["github-proxy"].get("allowed_repos") == ["acme/widgets"], bodies["github-proxy"]
    assert bodies["gitlab-proxy"].get("allowed_repos") == [], "the other forge's proxy must forward nothing"
    # The git-proxy has a grant of its own (#521), empty without git_upstream.
    assert bodies["git-proxy"].get("allowed_repos") == [] and "allowed_repos" not in bodies["xai-proxy"]
    assert _deletes(captured) == []


async def test_a_gitlab_job_scopes_gitlab_proxy_to_its_project(monkeypatch):
    pm, captured = _manager(monkeypatch)
    await _enroll(
        pm, "sb-2", github_token="glpat-SECRET", git_host="gitlab.example.com", forge="gitlab", repo="Group/Sub/Proj"
    )
    bodies = _posts(captured)
    assert bodies["gitlab-proxy"].get("allowed_repos") == ["group/sub/proj"], bodies["gitlab-proxy"]
    assert bodies["github-proxy"].get("allowed_repos") == [], "the other forge's proxy must forward nothing"


async def test_a_job_without_a_repo_gets_an_empty_scope_everywhere(monkeypatch):
    pm, captured = _manager(monkeypatch)
    await pm.enroll_sandbox("sb-3", forge="github")
    bodies = _posts(captured)
    for name in _SCOPED:
        assert bodies[name].get("allowed_repos") == [], (
            f"{name} enrolled without an explicit empty scope: {bodies[name]}"
        )


async def test_a_repo_the_forge_cannot_name_gets_an_empty_scope(monkeypatch):
    """Sending it would get a 400 and fail the sandbox; an empty scope loses nothing in use."""
    pm, captured = _manager(monkeypatch)
    with structlog.testing.capture_logs() as logs:
        await _enroll(pm, "sb-4", forge="github", repo="acme/widgets/extra")
    assert _posts(captured)["github-proxy"].get("allowed_repos") == []
    assert "forge_api_scope_repo_invalid" in [e["event"] for e in logs]


async def test_a_proxy_that_ignores_the_scope_is_revoked(monkeypatch):
    """An image older than scoping accepts the body, ignores the scope and forwards anything."""
    pm, captured = _manager(monkeypatch, echo=False)
    with structlog.testing.capture_logs() as logs:
        token = await pm.enroll_sandbox("sb-5", forge="github")
    assert sorted(_deletes(captured)) == sorted(_SCOPED), (
        f"every forge API proxy that did not confirm its scope must be unenrolled again, got {_deletes(captured)}"
    )
    deletes = [c for c in captured if c["method"] == "DELETE"]
    assert all(c["path"] == "/admin/enroll/sb-5" for c in deletes)
    assert token, "the sandbox itself is still enrolled with the other proxies"
    assert [e["proxy"] for e in logs if e["event"] == "forge_api_proxy_scope_unconfirmed"] == list(_SCOPED)


async def test_a_proxy_that_grants_a_different_scope_is_revoked(monkeypatch):
    pm, captured = _manager(monkeypatch, echo=False)

    async def wrong_echo(**kwargs):
        captured.append(kwargs)
        if kwargs["container"] == "git-proxy":
            return _git_proxy_echo(kwargs.get("body") or {})
        if kwargs["method"] == "POST" and kwargs["container"] == "github-proxy":
            return {"allowed_repos": ["acme/everything"]}
        return {"allowed_repos": (kwargs.get("body") or {}).get("allowed_repos", [])}

    monkeypatch.setattr(pm, "_exec_admin_request", wrong_echo)
    await _enroll(pm, "sb-6", forge="github", repo="acme/widgets")
    assert _deletes(captured) == ["github-proxy"]


async def test_a_stale_proxy_that_cannot_be_revoked_fails_the_enrollment(monkeypatch):
    """Revocation is the only thing keeping an unscoped bearer off that proxy."""
    pm, _ = _manager(monkeypatch, echo=False, fail_delete=True)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        await pm.enroll_sandbox("sb-7", forge="github")
