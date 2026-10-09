"""#466: a spent usage window is quarantined until the provider's reset, not
for the fixed quota interval."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embry0.safety.error_codes import ErrorCode
from embry0.services.credentials.health import CredentialHealth
from embry0.services.issue_executor import IssueExecutor

NOW = datetime(2026, 10, 2, 5, 15, tzinfo=UTC)
QUOTA = ErrorCode.PROVIDER_QUOTA_EXHAUSTED
RATE = ErrorCode.PROVIDER_RATE_LIMITED


class _Repo:
    def __init__(self) -> None:
        self.rows = {"cred-a": {"id": "cred-a", "label": "a", "status": "ok"}}
        self.status_calls: list[tuple[Any, ...]] = []

    async def get(self, credential_id: str) -> dict[str, Any] | None:
        row = self.rows.get(credential_id)
        return dict(row) if row else None

    async def set_status(self, credential_id, status, *, reason=None, quarantined_until=None):
        self.status_calls.append((credential_id, status, reason, quarantined_until))
        self.rows[credential_id].update(status=status, quarantined_until=quarantined_until)
        return dict(self.rows[credential_id])

    async def uses_for_job(self, job_id: str) -> list[dict[str, Any]]:
        return [{"role": "llm", "provider": "anthropic", "credential_id": "cred-a"}]

    async def finish_uses(self, job_id: str, error_code: str) -> int:
        return 1


def _health(repo: _Repo) -> CredentialHealth:
    cfg = SimpleNamespace(credential_rate_limit_quarantine_seconds=900, credential_quota_quarantine_seconds=3600)
    return CredentialHealth(repo, cfg, clock=lambda: NOW)


def test_the_cap_is_eight_days() -> None:
    from embry0.services.credentials.health import QUOTA_RESET_QUARANTINE_MAX

    assert timedelta(days=8) == QUOTA_RESET_QUARANTINE_MAX


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resets_at", "until"),
    [
        # Production: out of usage on 2026-10-02 05:15, reset Oct 5 00:00.
        (datetime(2026, 10, 5, 0, 0, tzinfo=UTC), datetime(2026, 10, 5, 0, 0, tzinfo=UTC)),
        # Sooner than the configured hour: the reset, not the hour.
        (NOW + timedelta(minutes=35), NOW + timedelta(minutes=35)),
        # Further out than any real window: capped.
        (NOW + timedelta(days=30), NOW + timedelta(days=8)),
        # Already past by the time the job ends: the window has turned over.
        (NOW - timedelta(minutes=1), NOW - timedelta(minutes=1)),
    ],
    ids=["days", "minutes", "capped", "past"],
)
async def test_quota_quarantine_lasts_until_the_reset(resets_at: datetime, until: datetime) -> None:
    repo = _Repo()
    await _health(repo).report("cred-a", QUOTA, resets_at=resets_at)
    assert repo.status_calls == [("cred-a", "quarantined", "quota_exhausted", until)], (
        f"expected quarantine until {until}, got {repo.status_calls}"
    )


@pytest.mark.asyncio
async def test_quota_without_a_reset_uses_the_configured_interval() -> None:
    repo = _Repo()
    await _health(repo).report("cred-a", QUOTA)
    assert repo.status_calls == [("cred-a", "quarantined", "quota_exhausted", NOW + timedelta(seconds=3600))], (
        f"no reset must fall back to CREDENTIAL_QUOTA_QUARANTINE_SECONDS, got {repo.status_calls}"
    )


@pytest.mark.asyncio
async def test_a_rate_limit_ignores_a_reset() -> None:
    """Only a spent window waits for the provider's reset; a rate limit keeps its short interval."""
    repo = _Repo()
    await _health(repo).report("cred-a", RATE, resets_at=NOW + timedelta(days=3))
    assert repo.status_calls == [("cred-a", "quarantined", "rate_limited", NOW + timedelta(seconds=900))], (
        f"a rate limit must keep CREDENTIAL_RATE_LIMIT_QUARANTINE_SECONDS, got {repo.status_calls}"
    )


@pytest.mark.asyncio
async def test_report_job_passes_the_reset_on() -> None:
    repo = _Repo()
    reset = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    ids = await _health(repo).report_job("job-1", QUOTA, "anthropic", resets_at=reset)
    assert ids == ["cred-a"]
    assert repo.status_calls[0][3] == reset, f"report_job must forward the reset, got {repo.status_calls}"


def _executor(repo: _Repo) -> IssueExecutor:
    ex = IssueExecutor.__new__(IssueExecutor)
    ex._jobs = MagicMock(update=AsyncMock(), get=AsyncMock(return_value=None))
    ex._issues = MagicMock(update=AsyncMock(), update_parent_status=AsyncMock())
    ex._db = None
    ex._audit_log_path = None
    ex._database_url = "postgresql://unused"
    ex._webhook_notifier = None
    ex._credentials_repo = repo
    ex._credential_health = _health(repo)
    return ex


def _failed_on(terminal: dict[str, Any]) -> dict[str, Any]:
    return {
        "triage_decision": {"action": "proceed"},
        "current_stage": "analysis_failed",
        "agent_outputs": [
            {"agent_type": "triage", "is_error": False, "output": "ok", "provider": "anthropic"},
            terminal,
        ],
        "errors": [f"{terminal['agent_type']}: {terminal['error_message']}"],
    }


async def _outcome(ex: IssueExecutor, result: dict[str, Any]) -> dict[str, Any]:
    with (
        patch("embry0.services.issue_executor.emit_audit", new=AsyncMock()),
        patch("embry0.orchestration.checkpoint.purge_thread", new=AsyncMock()),
    ):
        await ex._handle_workflow_result("iss-1", "job-1", result)
    return ex._jobs.update.await_args.kwargs


@pytest.mark.asyncio
async def test_job_outcome_quarantines_until_the_terminal_outputs_reset() -> None:
    repo = _Repo()
    reset = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    terminal = {
        "agent_type": "analysis",
        "is_error": True,
        "error_message": "You're out of extra usage · resets Oct 5, 12am (UTC)",
        "error_code": QUOTA.value,
        "provider_resets_at": reset.isoformat(),
        "provider": "anthropic",
        "output": "",
    }
    job = await _outcome(_executor(repo), _failed_on(terminal))
    assert job["error_code"] == QUOTA.value
    assert repo.status_calls == [("cred-a", "quarantined", "quota_exhausted", reset)], (
        f"the job outcome must quarantine until the run's reset, got {repo.status_calls}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stamp", [None, "", "not a time", "2026-10-05T00:00:00"], ids=["none", "empty", "junk", "naive"]
)
async def test_job_outcome_without_a_usable_reset_uses_the_interval(stamp: Any) -> None:
    repo = _Repo()
    terminal = {
        "agent_type": "analysis",
        "is_error": True,
        "error_message": "You're out of extra usage",
        "error_code": QUOTA.value,
        "provider_resets_at": stamp,
        "provider": "anthropic",
        "output": "",
    }
    await _outcome(_executor(repo), _failed_on(terminal))
    assert repo.status_calls == [("cred-a", "quarantined", "quota_exhausted", NOW + timedelta(seconds=3600))]
