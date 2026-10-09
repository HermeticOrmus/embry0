"""Forge routing: PR vs MR, and the git identity enrolled per sandbox (#167)."""

from types import SimpleNamespace

import pytest

from embry0.services._executor_outcomes import (
    _base_branch_for_change_request,
    _open_change_request,
)
from embry0.storage.repositories.repos import UnregisteredRepoError


class FakeRepos:
    def __init__(self, forge: str = "github", fail: bool = False) -> None:
        self._forge = forge
        self._fail = fail

    async def forge_for(self, repo: str) -> str:
        if self._fail:
            raise RuntimeError("db down")
        return self._forge


def _executor(forge_repo=None, *, gitlab_token: str = "", github_token: str = "ghp-x"):
    ex = SimpleNamespace()
    ex._repos = forge_repo
    ex._github_token = github_token
    ex._config = SimpleNamespace(gitlab_token=gitlab_token, gitlab_url="https://gitlab.com")
    return ex


async def test_github_repo_opens_a_pull_request(monkeypatch):
    called: dict = {}

    async def fake_pr(**kwargs):
        called.update(kwargs)
        return "https://github.com/o/r/pull/1"

    monkeypatch.setattr("embry0.services.github_prs.ensure_pr_for_branch", fake_pr)

    url = await _open_change_request(_executor(FakeRepos("github")), repo="o/r", branch="b", title="t", body="d")
    assert url == "https://github.com/o/r/pull/1"
    assert called["repo"] == "o/r"


async def test_gitlab_repo_opens_a_merge_request(monkeypatch):
    called: dict = {}

    async def fake_mr(**kwargs):
        called.update(kwargs)
        return "https://gitlab.com/g/p/-/merge_requests/2"

    monkeypatch.setattr("embry0.services.gitlab_mrs.ensure_mr_for_branch", fake_mr)

    url = await _open_change_request(
        _executor(FakeRepos("gitlab"), gitlab_token="glpat-x"),
        repo="acme/products/fbr",
        branch="b",
        title="t",
        body="d",
    )
    assert url == "https://gitlab.com/g/p/-/merge_requests/2"
    assert called["repo"] == "acme/products/fbr"


async def test_gitlab_without_a_token_is_skipped_not_attempted(monkeypatch):
    """Better a missing MR than a confusing 401 chain."""

    async def fake_mr(**kwargs):
        raise AssertionError("must not attempt without a token")

    monkeypatch.setattr("embry0.services.gitlab_mrs.ensure_mr_for_branch", fake_mr)
    assert (
        await _open_change_request(_executor(FakeRepos("gitlab")), repo="g/p", branch="b", title="t", body="d") is None
    )


@pytest.mark.parametrize("repos", [None, FakeRepos(fail=True)])
async def test_missing_or_broken_registry_falls_back_to_github(monkeypatch, repos):
    """Pre-#162 deployments meant github; a DB blip must not change the forge."""
    seen: dict = {}

    async def fake_pr(**kwargs):
        seen["hit"] = True
        return "https://github.com/o/r/pull/9"

    monkeypatch.setattr("embry0.services.github_prs.ensure_pr_for_branch", fake_pr)
    url = await _open_change_request(_executor(repos), repo="o/r", branch="b", title="t", body="d")
    assert url.endswith("/pull/9")
    assert seen["hit"] is True


class RefusingRepos:
    """A registry on a tenant that set DEFAULT_FORGE="" (Spec 1A §3.8)."""

    default_forge = ""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def forge_for(self, repo: str) -> str:
        if self._fail:
            raise RuntimeError("db down")
        raise UnregisteredRepoError(f"repo {repo!r} is not registered")


async def test_unregistered_repo_skips_the_change_request_instead_of_guessing_github(monkeypatch):
    """§3.8: on a tenant that refuses to guess, "github" would open a pull
    request on an unrelated public repo that happens to share the path."""

    async def fake_pr(**kwargs):
        raise AssertionError("must not open a pull request for an unregistered repo")

    async def fake_mr(**kwargs):
        raise AssertionError("must not open a merge request for an unregistered repo")

    monkeypatch.setattr("embry0.services.github_prs.ensure_pr_for_branch", fake_pr)
    monkeypatch.setattr("embry0.services.gitlab_mrs.ensure_mr_for_branch", fake_mr)

    got = await _open_change_request(
        _executor(RefusingRepos()),
        repo="acme/group/backend",
        branch="b",
        title="t",
        body="d",
    )
    assert got is None


async def test_registry_outage_with_no_default_forge_skips_rather_than_guessing(monkeypatch):
    """resolve_forge re-raises the infra error when there is no configured
    default to degrade to; the change request is skipped, not misrouted."""

    async def fake_pr(**kwargs):
        raise AssertionError("must not open a pull request without a resolved forge")

    monkeypatch.setattr("embry0.services.github_prs.ensure_pr_for_branch", fake_pr)
    got = await _open_change_request(_executor(RefusingRepos(fail=True)), repo="g/s/p", branch="b", title="t", body="d")
    assert got is None


async def test_base_branch_for_change_request_survives_an_unregistered_repo(monkeypatch):
    """Two things at once. It is awaited as an ARGUMENT to
    _open_change_request, so raising would abort _handle_workflow_result
    before the terminal job status is written and the job would hang in
    "running" forever. And with no forge resolved it must ask NO forge for a
    default branch — guessing github here is how a GitLab repo ends up
    targeting an unrelated GitHub project's "main"."""

    class Prefs:
        async def get(self, repo: str) -> dict | None:
            return None

    def no_client(**kwargs):
        raise AssertionError("no forge client may be built without a resolved forge")

    monkeypatch.setattr("embry0.execution.github_client.GitHubClient", no_client)
    monkeypatch.setattr("embry0.execution.gitlab_client.make_gitlab_client", no_client)

    ex = _executor(RefusingRepos(), gitlab_token="glpat-x")
    ex._repo_prefs = Prefs()
    got = await _base_branch_for_change_request(ex, "acme/group/backend", {})
    assert got == "main"


# --- sandbox git identity ---------------------------------------------------


def _sandbox_manager(gitlab_url: str = ""):
    from embry0.execution.sandbox_manager import SandboxManager

    return SandboxManager(
        docker=object(),
        proxy_manager=object(),
        github_token="ghp-x",
        gitlab_token="glpat-x",
        gitlab_url=gitlab_url,
    )


def test_github_enrols_no_explicit_identity():
    """None lets the proxy apply its own default, keeping old sandboxes exact."""
    assert _sandbox_manager()._git_identity("github") == (None, None)


def test_gitlab_enrols_gitlab_com_by_default():
    assert _sandbox_manager()._git_identity("gitlab") == ("gitlab.com", "oauth2")


def test_self_managed_host_is_extracted_from_the_url():
    """The credential host is a hostname, not a URL — scheme/path must go."""
    host, user = _sandbox_manager("https://git.internal.example/gitlab/")._git_identity("gitlab")
    assert host == "git.internal.example"
    assert user == "oauth2"


def test_gitlab_token_resolution_is_namespace_scoped(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN__ACME", "glpat-ns")
    mgr = _sandbox_manager()
    assert mgr._resolve_token_for_repo("acme/products/fbr", "gitlab") == "glpat-ns"
    # GitHub path is untouched by the GitLab env var.
    assert mgr._resolve_token_for_repo("acme/products/fbr", "github") == "ghp-x"


# --- Spec 1A §3.7: the fallback targets the job's base branch ---------------


async def test_pull_request_fallback_forwards_the_base_branch(monkeypatch):
    called: dict = {}

    async def fake_pr(**kwargs):
        called.update(kwargs)
        return "https://github.com/o/r/pull/3"

    monkeypatch.setattr("embry0.services.github_prs.ensure_pr_for_branch", fake_pr)
    await _open_change_request(
        _executor(FakeRepos("github")), repo="o/r", branch="b", title="t", body="d", target_branch="dev"
    )
    assert called["target_branch"] == "dev"


async def test_merge_request_fallback_forwards_the_base_branch(monkeypatch):
    called: dict = {}

    async def fake_mr(**kwargs):
        called.update(kwargs)
        return "https://gitlab.com/g/p/-/merge_requests/4"

    monkeypatch.setattr("embry0.services.gitlab_mrs.ensure_mr_for_branch", fake_mr)
    await _open_change_request(
        _executor(FakeRepos("gitlab"), gitlab_token="glpat-x"),
        repo="acme/group/backend",
        branch="b",
        title="t",
        body="d",
        target_branch="dev",
    )
    assert called["target_branch"] == "dev"


async def test_no_base_branch_means_ask_the_forge(monkeypatch):
    """An empty state value must become None, not the empty branch name."""
    called: dict = {}

    async def fake_pr(**kwargs):
        called.update(kwargs)
        return "https://github.com/o/r/pull/5"

    monkeypatch.setattr("embry0.services.github_prs.ensure_pr_for_branch", fake_pr)
    await _open_change_request(_executor(FakeRepos("github")), repo="o/r", branch="b", title="t", body="d")
    assert called["target_branch"] is None


# --- Spec 1A §3.7/§3.8: the call site never hands the forge an empty target --


def _outcome_executor():
    """An IssueExecutor with only what ``_handle_workflow_result`` touches."""
    from unittest.mock import AsyncMock, MagicMock

    from embry0.services.issue_executor import IssueExecutor

    ex = IssueExecutor.__new__(IssueExecutor)
    ex._jobs = MagicMock()
    ex._jobs.update = AsyncMock()
    ex._jobs.get = AsyncMock(return_value=None)
    ex._issues = MagicMock()
    ex._issues.update = AsyncMock()
    ex._issues.update_parent_status = AsyncMock()
    ex._db = None
    ex._audit_log_path = None
    ex._database_url = "postgresql://unused"
    ex._webhook_notifier = None
    return ex


async def _pr_fallback_kwargs(ex, **extra):
    """Drive a completed job that pushed a branch but reported no PR."""
    from unittest.mock import AsyncMock, patch

    opened = AsyncMock(return_value=None)
    with (
        patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()),
        patch("embry0.orchestration.checkpoint.purge_thread", new=AsyncMock()),
        patch("embry0.services._executor_outcomes._open_change_request", new=opened),
    ):
        await ex._handle_workflow_result(
            "iss-1",
            "job-1",
            {
                "triage_decision": {"action": "proceed"},
                "current_stage": "review_passed",
                "repo": "o/r",
                "task": "fix the widget",
                "branch_name": "embry0/iss-1-fix",
                "agent_outputs": [{"agent_type": "developer", "is_error": False, "output": "done"}],
                **extra,
            },
        )
    return opened.await_args.kwargs


class FakePrefs:
    def __init__(self, base_branch: str | None) -> None:
        self._base_branch = base_branch

    async def get(self, repo: str):
        return {"base_branch": self._base_branch}


async def test_call_site_forwards_the_jobs_resolved_base_branch():
    kwargs = await _pr_fallback_kwargs(_outcome_executor(), base_branch="dev")
    assert kwargs["target_branch"] == "dev"


async def test_a_resumed_job_resolves_the_base_branch_from_the_preference():
    """``result`` is a merge of node UPDATES: a resume never re-runs init, so
    base_branch is simply absent — and must not reach the forge as empty."""
    ex = _outcome_executor()
    ex._repo_prefs = FakePrefs("develop")
    kwargs = await _pr_fallback_kwargs(ex)
    assert kwargs["target_branch"] == "develop"


async def test_the_call_site_never_hands_the_forge_an_empty_target():
    """With nothing to resolve from, §3.7's own last step answers — not an
    empty string that would let the GitHub helper guess "main" silently."""
    kwargs = await _pr_fallback_kwargs(_outcome_executor())
    assert kwargs["target_branch"] == "main"
