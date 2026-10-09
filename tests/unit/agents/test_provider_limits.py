"""#466: reading the provider's out-of-usage sentence and its reset time."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from embry0.agents.provider_limits import parse_reset, usage_exhausted_sentence

# Verbatim from production event logs.
OUT_OF_USAGE_DAYS = "You're out of extra usage · resets Oct 5, 12am (UTC)"
OUT_OF_USAGE_HOURS = "You're out of extra usage · resets 12:50am (UTC)"


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "now", "expected"),
    [
        # The two production cases.
        (OUT_OF_USAGE_DAYS, _utc(2026, 10, 2, 5, 15), _utc(2026, 10, 5, 0, 0)),
        (OUT_OF_USAGE_HOURS, _utc(2026, 9, 28, 21, 11), _utc(2026, 9, 29, 0, 50)),
        # A time still ahead today stays today; one already past rolls to tomorrow.
        ("resets 11pm (UTC)", _utc(2026, 10, 2, 16, 0), _utc(2026, 10, 2, 23, 0)),
        ("resets 3pm (UTC)", _utc(2026, 10, 2, 16, 0), _utc(2026, 10, 3, 15, 0)),
        ("resets 4pm (UTC)", _utc(2026, 10, 2, 16, 0), _utc(2026, 10, 2, 16, 0)),
        ("resets 12pm (UTC)", _utc(2026, 10, 2, 1, 0), _utc(2026, 10, 2, 12, 0)),
        ("resets 3:05 PM (UTC)", _utc(2026, 10, 2, 1, 0), _utc(2026, 10, 2, 15, 5)),
        # A month and day already past this year roll to next year.
        ("resets Jan 3, 12am (UTC)", _utc(2026, 10, 2, 5, 15), _utc(2027, 1, 3, 0, 0)),
        ("resets Oct 1, 3pm (UTC)", _utc(2026, 10, 2, 5, 15), _utc(2027, 10, 1, 15, 0)),
        ("resets Oct 2, 12am (UTC)", _utc(2026, 10, 2, 5, 15), _utc(2027, 10, 2, 0, 0)),
        ("resets Feb 29, 12am (UTC)", _utc(2026, 10, 2, 5, 15), _utc(2028, 2, 29, 0, 0)),
        # The CLI adds the year when it differs from the current one.
        ("resets Jan 3, 2027, 12am (UTC)", _utc(2026, 12, 30, 0, 0), _utc(2027, 1, 3, 0, 0)),
        # A named zone is placed in that zone.
        ("resets 3pm (America/New_York)", _utc(2026, 10, 2, 12, 0), _utc(2026, 10, 2, 19, 0)),
        ("resets Oct 5, 12am (Etc/UTC)", _utc(2026, 10, 2, 5, 15), _utc(2026, 10, 5, 0, 0)),
    ],
)
def test_parse_reset(text: str, now: datetime, expected: datetime) -> None:
    got = parse_reset(text, now=now)
    assert got == expected, f"{text!r} at {now.isoformat()}: expected {expected.isoformat()}, got {got}"
    assert got is not None and got.utcoffset() is not None and got.utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "You're out of extra usage",
        "resets soon",
        "resets 12:50am",  # no zone: the time cannot be placed
        "resets 3pm (Mars/Olympus_Mons)",  # unknown zone
        "resets 25pm (UTC)",
        "resets 0am (UTC)",
        "resets 3:75pm (UTC)",
        "resets Foo 5, 12am (UTC)",
        "resets Feb 30, 12am (UTC)",
        "resets Oct 5 (UTC)",  # a date with no time is not a guess at midnight
        "resets in 5m",
    ],
)
def test_unparseable_reset_is_none(text: str | None) -> None:
    got = parse_reset(text, now=_utc(2026, 10, 2, 5, 15))
    assert got is None, f"{text!r} must not yield a reset, got {got}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (OUT_OF_USAGE_DAYS, OUT_OF_USAGE_DAYS),
        (OUT_OF_USAGE_HOURS, OUT_OF_USAGE_HOURS),
        (f"... Now the migration: {OUT_OF_USAGE_DAYS}", OUT_OF_USAGE_DAYS),
        (f"Step 1 is done.\n\n{OUT_OF_USAGE_HOURS}\n", OUT_OF_USAGE_HOURS),
        (
            "You\u2019ve hit your session limit · resets 3pm (UTC)",
            "You\u2019ve hit your session limit · resets 3pm (UTC)",
        ),
        ("You've reached your Opus limit.", "You've reached your Opus limit."),
        ("Your org is out of usage · contact your admin", "Your org is out of usage · contact your admin"),
        ("Claude AI usage limit reached|1759363200", "Claude AI usage limit reached|1759363200"),
    ],
)
def test_usage_exhausted_sentence(text: str, expected: str) -> None:
    assert usage_exhausted_sentence(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "API Error: Request rejected (429) · this may be a temporary capacity issue.",
        "rate_limit",
        "Reached maximum number of turns (40)",
    ],
)
def test_no_usage_wording_is_none(text: str | None) -> None:
    assert usage_exhausted_sentence(text) is None
