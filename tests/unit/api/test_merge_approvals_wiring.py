"""Production wires the merge-approval service into the executor (#319 phase 2).

The executor reads ``_merge_approvals`` with getattr, so without the
assignment every job still finishes normally and nobody is ever asked — the
#369 failure shape (an optional collaborator production never passes). And
the service's ``verifications``/``cases`` are optional too: without them the
ask silently omits the gate verdict and the decision skips the gate check.

Read from the AST of ``lifespan`` because that wiring needs Docker and a
database; see test_watcher_wiring.py for the same approach.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import embry0.api.app as app_module

REQUIRED = ("repo", "github", "notifier", "config", "verifications", "cases", "jobs")


def _tree() -> ast.Module:
    return ast.parse(pathlib.Path(app_module.__file__).read_text())


def _service_call() -> ast.Call:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "MergeApprovalService":
            return node
    pytest.fail("embry0/api/app.py never constructs MergeApprovalService")


def test_the_service_gets_every_collaborator() -> None:
    passed = {kw.arg: kw.value for kw in _service_call().keywords if kw.arg}
    missing = [name for name in REQUIRED if name not in passed]
    assert not missing, f"MergeApprovalService is built without {missing}"
    assert ast.unparse(passed["verifications"]).startswith("JobVerificationsRepository(")
    assert ast.unparse(passed["repo"]).startswith("MergeApprovalsRepository(")


def test_the_executor_is_handed_the_service() -> None:
    assigned = {
        ast.unparse(target)
        for node in ast.walk(_tree())
        if isinstance(node, ast.Assign)
        for target in node.targets
        if ast.unparse(node.value) == "app.state.merge_approvals"
    }
    assert "app.state.issue_executor._merge_approvals" in assigned, (
        "app.py never sets issue_executor._merge_approvals; jobs would finish and no ask would ever go out"
    )


def test_the_router_is_mounted_behind_auth() -> None:
    source = pathlib.Path(app_module.__file__).read_text()
    assert (
        'app.include_router(merge_approvals.router, prefix="/api/v1", tags=["merge-approvals"], '
        "dependencies=auth_deps)" in source
    )


def test_the_posting_sweep_is_started() -> None:
    """Asks are recorded silently and posted by the sweep; a service whose
    sweep never starts records asks that nobody ever sees."""
    started = [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "asyncio.create_task"
        and node.args
        and ast.unparse(node.args[0]) == "app.state.merge_approvals.start()"
    ]
    assert started, "app.py never starts the merge-approval sweep (app.state.merge_approvals.start())"


def test_the_job_row_reaches_the_service() -> None:
    passed = {kw.arg: kw.value for kw in _service_call().keywords if kw.arg}
    assert ast.unparse(passed["jobs"]) == "app.state.jobs_repo", "the reviewer verdict comes from the job row"


def test_an_empty_repo_list_is_warned_about() -> None:
    """The repo list defaults to empty, and empty asks about nothing — which
    is also exactly what a deployment that relied on the old non-empty
    default looks like. Startup must say so, or the asks stop silently."""
    warned_under = [
        ast.unparse(node.test)
        for node in ast.walk(_tree())
        if isinstance(node, ast.If)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and ast.unparse(call.func) == "logger.warning"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "merge_approvals_no_repos"
    ]
    assert "not app.state.merge_approvals.repos" in warned_under, (
        "app.py never warns `merge_approvals_no_repos` when the configured repo list is empty"
    )
