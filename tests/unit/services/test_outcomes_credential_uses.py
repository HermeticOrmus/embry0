import ast
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import embry0.services._executor_outcomes as _executor_outcomes
import embry0.services._executor_run as _executor_run
import embry0.services.issue_executor as issue_executor
from embry0.services.issue_executor import IssueExecutor


async def test_terminal_status_finishes_credential_uses():
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._credentials_repo = MagicMock(finish_uses=AsyncMock(return_value=1))
    await ex._finish_credential_uses("job-1", error_code=None)
    ex._credentials_repo.finish_uses.assert_awaited_once_with("job-1", "ok")
    await ex._finish_credential_uses("job-1", error_code="ERR_AGENT_TIMEOUT")
    ex._credentials_repo.finish_uses.assert_awaited_with("job-1", "ERR_AGENT_TIMEOUT")


async def test_finish_failure_is_swallowed():
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._credentials_repo = MagicMock(finish_uses=AsyncMock(side_effect=RuntimeError("db down")))
    await ex._finish_credential_uses("job-1", error_code=None)  # must not raise


async def test_no_repo_is_a_noop():
    ex = IssueExecutor.__new__(IssueExecutor)
    await ex._finish_credential_uses("job-1", error_code=None)


def test_constructor_stores_credentials_repo():
    repo = object()
    ex = IssueExecutor(
        issues_repo=MagicMock(),
        jobs_repo=MagicMock(),
        traces_repo=MagicMock(),
        workflow_registry=MagicMock(),
        database_url="postgres://stub",
        credentials_repo=repo,
    )
    assert ex._credentials_repo is repo, "IssueExecutor(credentials_repo=...) must store it as _credentials_repo"


# --- call sites: the ledger is stamped after the terminal status write --------


def _order_recording_executor():
    """Executor whose jobs.update and _finish_credential_uses append to one list."""
    order: list[tuple] = []
    ex = IssueExecutor.__new__(IssueExecutor)

    async def _update(job_id, **kw):
        order.append(("update", kw.get("status")))

    async def _finish(job_id, *, error_code):
        order.append(("finish", error_code))

    ex._jobs = MagicMock(update=AsyncMock(side_effect=_update), get=AsyncMock(return_value=None))
    ex._finish_credential_uses = AsyncMock(side_effect=_finish)
    return ex, order


async def test_split_completed_stamps_ok_after_write():
    ex, order = _order_recording_executor()
    ex._issues = MagicMock(
        get=AsyncMock(return_value={"priority": "low", "labels": [], "repo": "o/r"}),
        create=AsyncMock(return_value="child-1"),
        update=AsyncMock(),
        update_parent_status=AsyncMock(),
    )
    ex._emit_audit = AsyncMock()
    ex._db = None
    ex._audit_log_path = None

    await ex._handle_split("iss-1", "job-1", {"sub_tasks": [{"task": "a"}]})

    assert order == [("update", "completed"), ("finish", None)], order


async def test_needs_info_zero_questions_failed_stamps_error_code_after_write():
    ex, order = _order_recording_executor()
    ex._issues = MagicMock(get=AsyncMock(return_value={"id": "iss-1"}), update=AsyncMock())

    await ex._handle_needs_info("iss-1", "job-1", {"questions": [], "asking_node": "triage"})

    assert order == [("update", "failed"), ("finish", "ERR_EMPTY_INTERRUPT")], order


async def test_cancel_job_stamps_cancelled_after_cancelled_write(monkeypatch):
    ex, order = _order_recording_executor()
    ex._background_tasks = set()
    ex._tasks_by_job = {}
    ex._event_bus = None
    ex._database_url = "postgres://stub"
    ex._audit_log_path = None
    ex._db = MagicMock()
    ex._issues = MagicMock(update=AsyncMock())
    ex._sandbox = None
    ex._cleanup_job_volumes = AsyncMock()

    import embry0.orchestration.checkpoint as ckpt_mod

    monkeypatch.setattr(ckpt_mod, "purge_thread", AsyncMock())
    with patch.object(issue_executor, "emit_audit", new=AsyncMock()):
        await ex.cancel_job("job-1")

    assert order == [("update", "cancelled"), ("finish", "cancelled")], order


# --- structural guard: no terminal write escapes the stamp --------------------

_TERMINAL = {"completed", "failed", "cancelled", "partial"}


def _is_terminal_job_update(node: ast.AST) -> bool:
    """`await self._jobs.update(..., status=<terminal literal | variable>)`."""
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Await)):
        return False
    call = node.value.value
    if not (isinstance(call, ast.Call) and ast.unparse(call.func) == "self._jobs.update"):
        return False
    for kw in call.keywords:
        if kw.arg == "status":
            if isinstance(kw.value, ast.Constant):
                return kw.value.value in _TERMINAL
            return isinstance(kw.value, ast.Name)  # final_status / job_status
    return False


def _is_finish_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Await)
        and isinstance(node.value.value, ast.Call)
        and ast.unparse(node.value.value.func) == "self._finish_credential_uses"
    )


def test_every_terminal_status_write_is_followed_by_the_ledger_stamp():
    missing = []
    found = 0
    for module in (_executor_outcomes, _executor_run, issue_executor):
        tree = ast.parse(inspect.getsource(module))
        for parent in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(parent, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block):
                    if _is_terminal_job_update(stmt):
                        found += 1
                        nxt = block[i + 1] if i + 1 < len(block) else None
                        if nxt is None or not _is_finish_call(nxt):
                            missing.append(f"{module.__name__}:{stmt.lineno}")
    assert found >= 15, f"guard matched only {found} terminal writes; the matcher is broken"
    assert not missing, f"terminal job status writes not followed by _finish_credential_uses: {missing}"
