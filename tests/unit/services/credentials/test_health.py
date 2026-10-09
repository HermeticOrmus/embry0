"""CredentialHealth is the only writer of quarantined (spec §3.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import structlog

from embry0.safety.error_codes import ErrorCode
from embry0.services.credentials.health import CredentialHealth

WHEN = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class Repo:
    def __init__(self, rows, uses=None):
        self.rows = rows
        self.uses = uses or {}
        self.status_calls = []

    async def get(self, credential_id):
        row = self.rows.get(credential_id)
        return dict(row) if row else None

    async def set_status(self, credential_id, status, *, reason=None, quarantined_until=None):
        self.status_calls.append((credential_id, status, reason, quarantined_until))
        self.rows[credential_id].update(status=status, status_reason=reason, quarantined_until=quarantined_until)
        return dict(self.rows[credential_id])

    async def uses_for_job(self, job_id):
        return list(self.uses.get(job_id, []))


def _health(repo, *, rate=900, quota=3600):
    cfg = SimpleNamespace(
        credential_rate_limit_quarantine_seconds=rate,
        credential_quota_quarantine_seconds=quota,
    )
    return CredentialHealth(repo, cfg, clock=lambda: WHEN)


def _row(label, status="ok"):
    return {"id": f"cred-{label}", "label": label, "status": status, "secret_ciphertext": "enc:super-secret-value"}


async def test_rate_limit_quota_and_auth_set_quarantine():
    repo = Repo({"cred-a": _row("a"), "cred-b": _row("b"), "cred-c": _row("c")})
    health = _health(repo)
    with structlog.testing.capture_logs() as logs:
        rate = await health.report("cred-a", ErrorCode.PROVIDER_RATE_LIMITED)
        quota = await health.report("cred-b", ErrorCode.PROVIDER_QUOTA_EXHAUSTED)
        auth = await health.report("cred-c", ErrorCode.AUTH_REJECTED)
    assert rate["status"] == "quarantined"
    assert repo.status_calls[0] == ("cred-a", "quarantined", "rate_limited", WHEN + timedelta(seconds=900))
    assert repo.status_calls[1] == ("cred-b", "quarantined", "quota_exhausted", WHEN + timedelta(seconds=3600))
    assert repo.status_calls[2] == ("cred-c", "quarantined", "auth_rejected", None)
    assert auth["quarantined_until"] is None and quota["status_reason"] == "quota_exhausted"
    rendered = str(logs)
    assert "super-secret-value" not in rendered
    assert all(e["event"] == "credential_quarantined" for e in logs)
    assert logs[0]["reason"] == "rate_limited" and logs[0]["credential_id"] == "cred-a"


async def test_other_codes_and_disabled_rows_are_left_alone():
    repo = Repo({"cred-ok": _row("ok"), "cred-off": _row("off", status="disabled")})
    health = _health(repo)
    assert await health.report("cred-ok", ErrorCode.AGENT_PROCESS_FAILED) is None
    assert await health.report("cred-off", ErrorCode.PROVIDER_RATE_LIMITED) is None
    assert await health.report("cred-missing", ErrorCode.AUTH_REJECTED) is None
    assert repo.status_calls == []
    assert repo.rows["cred-off"]["status"] == "disabled"


async def test_report_job_filters_by_role_and_provider():
    repo = Repo(
        {"cred-x": _row("x"), "cred-a": _row("a"), "cred-off": _row("off", status="disabled")},
        uses={
            "job-1": [
                {"role": "forge", "provider": "github", "credential_id": "cred-x"},
                {"role": "llm", "provider": "anthropic", "credential_id": "cred-a"},
                {"role": "llm", "provider": "xai", "credential_id": "cred-off"},
            ]
        },
    )
    ids = await _health(repo).report_job("job-1", ErrorCode.PROVIDER_RATE_LIMITED, "anthropic")
    assert ids == ["cred-a"]
    assert [call[0] for call in repo.status_calls] == ["cred-a"]
    all_ids = await _health(repo).report_job("job-1", ErrorCode.PROVIDER_QUOTA_EXHAUSTED, None)
    assert all_ids == ["cred-a"]
