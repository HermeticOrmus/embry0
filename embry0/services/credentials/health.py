"""The only writer of ``status='quarantined'`` (spec 2026-09-29 §3.6).

``disabled`` stays operator-only. A past ``quarantined_until`` is usable
again without a rewrite; the next report or an operator clear updates the row.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from embry0.safety.error_codes import XAI_DENIAL_CODES, ErrorCode
from embry0.safety.xai_denials import XaiDenial, classify_xai_denial

if TYPE_CHECKING:
    from embry0.storage.repositories.credentials import CredentialsRepository

logger = structlog.get_logger(__name__)

_REASONS: dict[str, tuple[str, str | None]] = {
    ErrorCode.PROVIDER_RATE_LIMITED: ("rate_limited", "credential_rate_limit_quarantine_seconds"),
    ErrorCode.PROVIDER_QUOTA_EXHAUSTED: ("quota_exhausted", "credential_quota_quarantine_seconds"),
    ErrorCode.AUTH_REJECTED: ("auth_rejected", None),
}

# Quarantine reasons for an xAI 403 whose body named its condition: what an
# operator reads on the credentials page instead of the code's generic reason.
_XAI_DENIAL_REASONS: dict[XaiDenial, str] = {
    XaiDenial.SPENDING_LIMIT: "spending_limit",
    XaiDenial.NO_SUBSCRIPTION: "no_active_subscription",
}


def xai_quarantine_reason(provider: str | None, code: str, provider_text: str) -> str | None:
    """The reason to quarantine an xAI account with, when the run's error is an xAI 403
    that named its condition and ``code`` is the one that condition maps to; else None."""
    if provider != "xai":
        return None
    denial = classify_xai_denial(provider_text)
    if denial is None or denial not in _XAI_DENIAL_REASONS or XAI_DENIAL_CODES[denial].value != code:
        return None
    return _XAI_DENIAL_REASONS[denial]


# #466: the longest a provider's stated reset may hold a spent account out.
# The longest window the Claude CLI reports is seven days; one more day of
# slack covers a reset that lands just after a full window. A reset further
# out is more likely a misread than a real date, and retrying too early costs
# one failed run before the account is quarantined again.
QUOTA_RESET_QUARANTINE_MAX = timedelta(days=8)


class CredentialHealth:
    def __init__(
        self,
        repo: CredentialsRepository,
        config: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repo
        self._config = config
        self._clock = clock

    def _until(self, code: str, window_attr: str, resets_at: datetime | None) -> datetime:
        """End of a timed quarantine. A spent usage window lasts until the
        reset the provider named, capped at QUOTA_RESET_QUARANTINE_MAX. A rate
        limit, and a spent window with no known reset, use the configured
        interval. A reset already past ends the quarantine at once: the
        window has turned over."""
        now = self._clock()
        if code == ErrorCode.PROVIDER_QUOTA_EXHAUSTED and resets_at is not None:
            return min(resets_at, now + QUOTA_RESET_QUARANTINE_MAX)
        return now + timedelta(seconds=int(getattr(self._config, window_attr)))

    async def report(
        self, credential_id: str, code: str, *, resets_at: datetime | None = None, reason: str | None = None
    ) -> dict[str, Any] | None:
        """Quarantine ``credential_id`` for ``code``. ``resets_at`` (aware) is
        the provider's reset of a spent usage window, when it named one.
        ``reason`` replaces the code's default reason; the code still decides how long."""
        spec = _REASONS.get(code)
        if spec is None:
            return None
        row = await self._repo.get(credential_id)
        if row is None or row.get("status") == "disabled":
            return None
        default_reason, window_attr = spec
        reason = reason or default_reason
        until = None
        if window_attr is not None:
            until = self._until(code, window_attr, resets_at)
        updated = await self._repo.set_status(credential_id, "quarantined", reason=reason, quarantined_until=until)
        logger.info(
            "credential_quarantined",
            credential_id=credential_id,
            label=row.get("label"),
            reason=reason,
            until=until.isoformat() if until is not None else None,
        )
        return updated

    async def report_job(
        self,
        job_id: str,
        code: str,
        provider: str | None,
        *,
        resets_at: datetime | None = None,
        reason: str | None = None,
    ) -> list[str]:
        quarantined: list[str] = []
        for use in await self._repo.uses_for_job(job_id):
            if use.get("role") != "llm":
                continue
            if provider is not None and use.get("provider") != provider:
                continue
            credential_id = str(use["credential_id"])
            if await self.report(credential_id, code, resets_at=resets_at, reason=reason) is not None:
                quarantined.append(credential_id)
        return quarantined
