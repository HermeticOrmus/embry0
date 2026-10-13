"""#360: a dispatch to a repo with no ``repos`` row is refused, whatever
DEFAULT_FORGE says — and a repo registered under a REPO_RENAMES spelling is
still registered.

Before, ``ERR_REPO_UNREGISTERED`` fired only when DEFAULT_FORGE was empty, so
on a default deployment a Linear `repo:` stamp naming an unregistered (or
renamed) repo dispatched a job with no QA config, env vars or secrets.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.safety.error_codes import ErrorCode
from embry0.services.issue_executor import IssueExecutor
from embry0.services.linear_sync import LinearSyncService, LinearTriggerIssue
from embry0.storage import repo_renames
from embry0.storage.repositories.issues import IssuesRepository
from embry0.storage.repositories.jobs import JobsRepository
from embry0.storage.repositories.repos import DEFAULT_FORGE, ReposRepository, UnregisteredRepoError
from embry0.trackers import TriggerContext

_db = [pytest.mark.requires_postgres, pytest.mark.asyncio]


def db_test(fn):
    for mark in _db:
        fn = mark(fn)
    return fn


RENAMES = {"acme/old-name": "acme/new-name"}


def _ticket(stamp: str) -> LinearTriggerIssue:
    return LinearTriggerIssue(
        tracker="linear",
        native_id="uuid-1",
        key="ENG-1",
        title="Fix the thing",
        body="body",
        url="https://linear.test/ENG-1",
        labels=["embry0"],
        state_type="unstarted",
        project="Backend",
        project_id="proj-1",
        project_summary=f"repo: {stamp}",
        project_content="",
        team_key="ENG",
    )


def _executor(db, issues: IssuesRepository, jobs: JobsRepository) -> IssueExecutor:
    """The real execute() up to the job row; the workflow run is dropped."""
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._issues = issues
    ex._jobs = jobs
    ex._repos = ReposRepository(db)
    ex._db = None
    ex._audit_log_path = None
    ex._case_recorder = None
    ex._track_task = lambda coro, **_: coro.close()
    return ex


# ---- the registry lookup -----------------------------------------------------


@db_test
async def test_registered_as_honours_case_and_repo_renames(db_with_migrations, monkeypatch) -> None:
    repos = ReposRepository(db_with_migrations)
    await repos.register("acme/new-name", forge="github")
    await repos.register("acme/registered-before-rename", forge="github")
    assert await repos.registered_as("acme/new-name") == "acme/new-name"
    assert await repos.registered_as("ACME/New-Name") == "acme/new-name"
    assert await repos.registered_as("acme/old-name") is None, "no rename configured yet"

    monkeypatch.setattr(
        repo_renames,
        "REPO_RENAMES",
        {**RENAMES, "acme/registered-before-rename": "acme/renamed-since"},
    )
    # A stamp still naming the old slug finds the row under the current one ...
    assert await repos.registered_as("acme/old-name") == "acme/new-name"
    # ... and a stamp naming the current slug finds a row kept under the old one.
    assert await repos.registered_as("acme/renamed-since") == "acme/registered-before-rename"
    assert await repos.registered_as("acme/never-registered") is None


# ---- the executor: every dispatch ends here ------------------------------------


@db_test
async def test_execute_refuses_an_unregistered_repo_with_the_default_forge_set(db_with_migrations) -> None:
    assert DEFAULT_FORGE == "github", "the gap was that a set DEFAULT_FORGE let the dispatch through"
    issues, jobs = IssuesRepository(db_with_migrations), JobsRepository(db_with_migrations)
    issue_id = await issues.create(title="t", repo="acme/never-registered")
    ex = _executor(db_with_migrations, issues, jobs)

    with pytest.raises(UnregisteredRepoError) as refused:
        await ex.execute(issue_id)

    assert str(refused.value).startswith(ErrorCode.REPO_UNREGISTERED.value), str(refused.value)
    assert "acme/never-registered" in str(refused.value)
    _rows, total = await jobs.list_all(issue_id=issue_id)
    assert total == 0, "a job row was created for an unregistered repo"


# ---- the Linear trigger: refused before anything is written, and said so ------


async def _linear(db, *, stamp: str) -> tuple[dict[str, Any], LinearSyncService, list[str], JobsRepository]:
    issues, jobs = IssuesRepository(db), JobsRepository(db)
    svc = LinearSyncService("lin_api_key", {})
    svc.fetch_trigger_issue = AsyncMock(return_value=_ticket(stamp))  # type: ignore[method-assign]
    comments: list[str] = []

    async def _post(_issue_id: str, body: str) -> bool:
        comments.append(body)
        return True

    svc.post_comment = _post  # type: ignore[method-assign]
    from embry0.storage.repositories.dispatch_ledger import DispatchLedgerRepository

    ctx = TriggerContext(
        issues_repo=issues,
        issue_executor=_executor(db, issues, jobs),
        jobs_repo=jobs,
        dispatch_ledger=DispatchLedgerRepository(db),
        trigger_labels={"embry0"},
    )
    payload = {"type": "Issue", "action": "create", "data": {"id": "uuid-1", "identifier": "ENG-1"}}
    result = await svc.handle_webhook_event(payload, ctx)
    return result, svc, comments, jobs


@db_test
async def test_a_linear_stamp_naming_an_unregistered_repo_is_refused_and_told(db_with_migrations) -> None:
    result, _svc, comments, jobs = await _linear(db_with_migrations, stamp="acme/never-registered")

    assert result.get("skip") == "unregistered_repo", f"an unregistered repo was dispatched: {result}"
    assert result.get("error_code") == ErrorCode.REPO_UNREGISTERED.value
    _rows, total = await jobs.list_all()
    assert total == 0
    assert len(comments) == 1 and "acme/never-registered" in comments[0] and "not registered" in comments[0]

    again, _svc, comments_again, _jobs = await _linear(db_with_migrations, stamp="acme/never-registered")
    assert again.get("skip") == "unregistered_repo"
    assert comments_again == [], "the refusal was commented twice on the same ticket"


@db_test
async def test_a_linear_stamp_naming_the_old_slug_of_a_registered_repo_dispatches(
    db_with_migrations, monkeypatch
) -> None:
    """Production has repos registered under the current name while a project
    stamp still names the old one (and the reverse). REPO_RENAMES says they are
    the same repo, so the dispatch must go ahead."""
    monkeypatch.setattr(repo_renames, "REPO_RENAMES", dict(RENAMES))
    await ReposRepository(db_with_migrations).register("acme/new-name", forge="github")

    result, _svc, _comments, jobs = await _linear(db_with_migrations, stamp="acme/old-name")

    assert result.get("action") == "dispatched", f"a renamed, registered repo was refused: {result}"
    _rows, total = await jobs.list_all()
    assert total == 1


@db_test
async def test_a_registered_repo_dispatches(db_with_migrations) -> None:
    await ReposRepository(db_with_migrations).register("acme/widgets", forge="github")
    result, _svc, _comments, _jobs = await _linear(db_with_migrations, stamp="acme/widgets")
    assert result.get("action") == "dispatched", result


# ---- production wiring --------------------------------------------------------


def test_production_wires_the_registry_into_the_executor_and_the_watcher() -> None:
    """The refusal reads IssueExecutor._repos; without it every repo passes
    silently (CLAUDE.md, #369). The watcher asks the same executor."""
    import ast
    import pathlib

    import embry0.api.app as app_module

    tree = ast.parse(pathlib.Path(app_module.__file__).read_text())
    calls = {
        node.func.id: node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("IssueExecutor",)
    }
    passed = {kw.arg: ast.unparse(kw.value) for kw in calls["IssueExecutor"].keywords if kw.arg}
    assert passed.get("repos_repo") == "app.state.repos_repo", passed.get("repos_repo")
    watcher = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "WatcherService"
    ]
    assert watcher, "no WatcherService construction in app.py"
    for node in watcher:
        executor = {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}.get("executor")
        assert executor == "app.state.issue_executor", executor


@pytest.mark.asyncio
async def test_the_executor_refuses_nothing_without_a_registry() -> None:
    """Unit graphs build an executor with no registry; that must stay a pass."""
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._repos = None
    assert await ex.unregistered_repo_refusal("acme/x") is None
    ex._repos = MagicMock(registered_as=AsyncMock(side_effect=RuntimeError("db down")))
    assert await ex.unregistered_repo_refusal("acme/x") is None
