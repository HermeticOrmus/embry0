"""A provider failure quarantines the account that served it (spec §3.6).

Review focus: two llm uses, a terminal 429 attributed to xai, only that row
is quarantined, and the credential-use ledger carries the same code.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embry0.safety.error_codes import ErrorCode
from embry0.services.credentials.health import CredentialHealth
from embry0.services.issue_executor import IssueExecutor

RATE = ErrorCode.PROVIDER_RATE_LIMITED.value
QUOTA = ErrorCode.PROVIDER_QUOTA_EXHAUSTED.value
AUTH = ErrorCode.AUTH_REJECTED.value
STALE = ErrorCode.PROVIDER_TOKEN_STALE.value


class _Repo:
    def __init__(self) -> None:
        self.rows = {
            "cred-xai": {"id": "cred-xai", "label": "grok-a", "status": "active"},
            "cred-anthropic": {"id": "cred-anthropic", "label": "claude-b", "status": "active"},
        }
        self.uses = {
            "job-1": [
                {"role": "llm", "provider": "xai", "credential_id": "cred-xai"},
                {"role": "llm", "provider": "anthropic", "credential_id": "cred-anthropic"},
                {"role": "forge", "provider": "github", "credential_id": "cred-forge"},
            ]
        }
        self.status_calls: list[tuple] = []
        self.finished: list[tuple] = []

    async def get(self, credential_id: str):
        row = self.rows.get(credential_id)
        return dict(row) if row else None

    async def set_status(self, credential_id, status, *, reason=None, quarantined_until=None):
        self.status_calls.append((credential_id, status, reason))
        self.rows[credential_id].update(status=status, status_reason=reason, quarantined_until=quarantined_until)
        return dict(self.rows[credential_id])

    async def uses_for_job(self, job_id: str):
        return list(self.uses.get(job_id, []))

    async def finish_uses(self, job_id: str, error_code: str):
        self.finished.append((job_id, error_code))
        return 1


def _health(repo: _Repo) -> CredentialHealth:
    cfg = SimpleNamespace(
        credential_rate_limit_quarantine_seconds=900,
        credential_quota_quarantine_seconds=3600,
    )
    return CredentialHealth(repo, cfg)


def _executor(repo: _Repo | None, health: CredentialHealth | None):
    ex = IssueExecutor.__new__(IssueExecutor)
    order: list[str] = []

    async def _update(job_id, **kw):
        order.append("update")
        return None

    ex._jobs = MagicMock(update=AsyncMock(side_effect=_update), get=AsyncMock(return_value=None))
    ex._issues = MagicMock(update=AsyncMock(), update_parent_status=AsyncMock())
    ex._db = None
    ex._audit_log_path = None
    ex._database_url = "postgresql://unused"
    ex._webhook_notifier = None
    ex._credentials_repo = repo
    ex._credential_health = health
    ex._order = order
    return ex


def _failed(provider: str | None, code: str) -> dict:
    terminal: dict = {
        "agent_type": "developer",
        "is_error": True,
        "error_message": "nope",
        "error_code": code,
        "output": "",
    }
    if provider is not None:
        terminal["provider"] = provider
    return {
        "triage_decision": {"action": "proceed"},
        "current_stage": "developer_failed",
        "agent_outputs": [
            {"agent_type": "triage", "is_error": False, "output": "ok", "provider": "anthropic"},
            terminal,
        ],
    }


async def _run(ex, result):
    with (
        patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()),
        patch("embry0.orchestration.checkpoint.purge_thread", new=AsyncMock()),
    ):
        await ex._handle_workflow_result("iss-1", "job-1", result)
    return ex._jobs.update.await_args.kwargs


@pytest.mark.asyncio
async def test_xai_rate_limit_quarantines_only_the_xai_account():
    """Two llm uses. The terminal 429 names xai. Only cred-xai is quarantined."""
    repo = _Repo()
    ex = _executor(repo, _health(repo))
    kwargs = await _run(ex, _failed("xai", RATE))
    assert kwargs["status"] == "failed"
    assert kwargs["error_code"] == RATE, "the job row must carry ERR_PROVIDER_RATE_LIMITED"
    assert repo.finished == [("job-1", RATE)], "the credential-use ledger must carry the rate-limit code"
    assert [call[0] for call in repo.status_calls] == ["cred-xai"], (
        "only cred-xai is quarantined; cred-anthropic served a different provider"
    )
    assert repo.status_calls[0][1:] == ("quarantined", "rate_limited")
    assert ex._order == ["update"], "the job row is written before the quarantine"
    assert repo.rows["cred-anthropic"]["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "code", "reason", "kept"),
    [
        ("anthropic", QUOTA, "quota_exhausted", "cred-xai"),
        ("xai", AUTH, "auth_rejected", "cred-anthropic"),
    ],
)
async def test_quota_and_auth_quarantine_only_the_named_provider(provider, code, reason, kept):
    repo = _Repo()
    ex = _executor(repo, _health(repo))
    await _run(ex, _failed(provider, code))
    quarantined = [call[0] for call in repo.status_calls]
    assert quarantined == [f"cred-{provider}"], f"expected only cred-{provider} quarantined, got {quarantined}"
    assert repo.status_calls[0][2] == reason
    assert repo.rows[kept]["status"] == "active"
    assert repo.finished == [("job-1", code)]


@pytest.mark.asyncio
async def test_a_stale_xai_token_fails_the_job_without_quarantine():
    """xAI called the access token stale. The token loop force-refreshes it; benching the
    only grok account with no end would stop every grok job until an operator stepped in."""
    repo = _Repo()
    ex = _executor(repo, _health(repo))
    kwargs = await _run(ex, _failed("xai", STALE))
    assert kwargs["error_code"] == STALE, "the job row must carry ERR_PROVIDER_TOKEN_STALE"
    assert repo.status_calls == [], "a stale token must not quarantine the account"
    assert repo.finished == [("job-1", STALE)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "text", "reason"),
    [
        (AUTH, "Error code: 403 - {'error': 'You do not have an active Grok subscription.'}", "no_active_subscription"),
        (
            QUOTA,
            "Error code: 403 - {'code': 'personal-team-blocked:spending-limit', 'error': 'limit reached'}",
            "spending_limit",
        ),
    ],
)
async def test_an_xai_403_quarantines_with_its_own_reason(code, text, reason):
    """The reason is what an operator reads on the credentials page."""
    repo = _Repo()
    ex = _executor(repo, _health(repo))
    result = _failed("xai", code)
    result["agent_outputs"][-1]["error_message"] = text
    await _run(ex, result)
    assert repo.status_calls == [("cred-xai", "quarantined", reason)], repo.status_calls


@pytest.mark.asyncio
async def test_a_non_provider_failure_does_not_quarantine():
    repo = _Repo()
    ex = _executor(repo, _health(repo))
    kwargs = await _run(ex, _failed("xai", ErrorCode.AGENT_PROCESS_FAILED.value))
    assert kwargs["error_code"] == ErrorCode.AGENT_PROCESS_FAILED.value
    assert repo.status_calls == [], "ERR_AGENT_PROCESS_FAILED must not quarantine an account"
    assert repo.finished == [("job-1", ErrorCode.AGENT_PROCESS_FAILED.value)]


@pytest.mark.asyncio
async def test_missing_health_still_finishes_the_job():
    repo = _Repo()
    ex = _executor(repo, None)
    kwargs = await _run(ex, _failed("xai", RATE))
    assert kwargs["status"] == "failed"
    assert kwargs["error_code"] == RATE
    assert repo.status_calls == [], "no health object means no quarantine, not a crash"
    assert repo.finished == [("job-1", RATE)]


@pytest.mark.asyncio
async def test_a_quarantine_write_failure_leaves_the_job_row():
    repo = _Repo()
    health = _health(repo)

    async def _boom(*_a, **_k):
        raise RuntimeError("db down")

    health.report_job = _boom  # type: ignore[method-assign]
    ex = _executor(repo, health)
    kwargs = await _run(ex, _failed("xai", RATE))
    assert kwargs["status"] == "failed", "a quarantine failure must not flip the already-written job row"
    assert kwargs["error_code"] == RATE


def test_constructor_stores_credential_health():
    health = object()
    ex = IssueExecutor(
        issues_repo=MagicMock(),
        jobs_repo=MagicMock(),
        traces_repo=MagicMock(),
        workflow_registry=MagicMock(),
        database_url="postgres://stub",
        credential_health=health,
    )
    assert ex._credential_health is health, "IssueExecutor(credential_health=...) must store it"
