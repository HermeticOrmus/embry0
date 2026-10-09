"""#466: the text classifier reads a spent usage window as quota, not as a rate limit."""

from __future__ import annotations

import pytest

from embry0.safety.error_codes import ErrorCode, classify_agent_error

QUOTA = ErrorCode.PROVIDER_QUOTA_EXHAUSTED
RATE = ErrorCode.PROVIDER_RATE_LIMITED


@pytest.mark.parametrize(
    "text",
    [
        # Verbatim from production event logs.
        "You're out of extra usage · resets Oct 5, 12am (UTC)",
        "You're out of extra usage · resets 12:50am (UTC)",
        # As the job's errors list carries it.
        "triage_agent_error: You're out of extra usage · resets Oct 5, 12am (UTC)",
        # The CLI's sibling messages for the same condition.
        "You're out of usage credits · resets Nov 1, 12am (UTC)",
        "Your org is out of usage · contact your admin",
        "You've hit your session limit · resets 3pm (UTC)",
        "You\u2019ve hit your weekly limit · resets Oct 5, 12am (UTC)",
        "You've reached your Opus limit.",
        "Claude AI usage limit reached|1759363200",
        # A spent window whose message also says rate limit / 429.
        "rate limit: You're out of extra usage (429)",
    ],
)
def test_spent_usage_window_is_quota_exhausted(text: str) -> None:
    got = classify_agent_error(text)
    assert got == QUOTA, f"{text!r} must be {QUOTA.value}, got {got}"


@pytest.mark.parametrize(
    "text",
    [
        "API Error: Request rejected (429) · this may be a temporary capacity issue.",
        "rate limit exceeded",
        "Error code: 429 - {'error': {'type': 'rate_limit_error'}}",
    ],
)
def test_transient_rate_limit_stays_rate_limited(text: str) -> None:
    got = classify_agent_error(text)
    assert got == RATE, f"{text!r} must stay {RATE.value}, got {got}"
