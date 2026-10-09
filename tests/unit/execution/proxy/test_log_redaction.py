"""Log-line redaction (SH-P4.4): what reaches a third-party model provider."""

from __future__ import annotations

import pytest

from embry0.execution.proxy.log_redaction import (
    REDACT_OVERLAP_CHARS,
    REDACTED,
    redact_bounded,
    redact_log_text,
)

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("GET /x Authorization: Bearer abc123def456ghi789", "abc123def456ghi789"),
        ("authorization=Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("upstream said Bearer zzTOKENzz1234567890 was bad", "zzTOKENzz1234567890"),
        ("X-Api-Key: 9f8e7d6c5b4a", "9f8e7d6c5b4a"),
        ("Cookie: session=abcdef; theme=dark", "abcdef"),
        ('{"password": "hunter2", "user": "bob"}', "hunter2"),
        ("db_password=s3cr3t! retrying", "s3cr3t!"),
        ("client_secret: 'q1w2e3r4'", "q1w2e3r4"),
        ("APOLLO_API_KEY=ak_live_12345 rejected with 401", "ak_live_12345"),
        ("refresh_token=1//0gabcdefg", "1//0gabcdefg"),
        (f"token rejected: {_JWT}", _JWT),
        ("key sk-ant-REDACTME-abcdefghijklmnop was revoked", "sk-ant-REDACTME-abcdefghijklmnop"),
        ("clone with ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("aws AKIAIOSFODNN7EXAMPLE denied", "AKIAIOSFODNN7EXAMPLE"),
        ("postgresql://app:p%40ssw0rd@db:5432/app refused", "p%40ssw0rd"),
        ("mailbox jane.doe+x@example.com returned 403", "jane.doe+x@example.com"),
        ("digest 0123456789abcdef0123456789abcdef01234567 mismatch", "0123456789abcdef0123456789abcdef01234567"),
    ],
)
def test_secret_or_pii_is_removed(raw: str, secret: str) -> None:
    out = redact_log_text(raw)
    assert secret not in out
    assert REDACTED in out or "<email>" in out


def test_diagnostic_content_survives() -> None:
    raw = (
        "2026-09-09T10:00:01Z acme-backend ERROR ApolloClient 401 Unauthorized: invalid key "
        "at com.acme.Apollo.fetch(Apollo.java:88) status=401 attempt=3 path=/api/v1/leads/42"
    )
    assert redact_log_text(raw) == raw


def test_key_names_are_kept_so_the_line_still_reads() -> None:
    assert redact_log_text("db_password=s3cr3t host=db1") == f"db_password={REDACTED} host=db1"
    assert redact_log_text('{"password": "hunter2", "user": "bob"}') == f'{{"password": "{REDACTED}", "user": "bob"}}'


def test_url_userinfo_keeps_host() -> None:
    assert redact_log_text("postgresql://app:pw@db:5432/app") == f"postgresql://{REDACTED}@db:5432/app"


def test_idempotent_and_total() -> None:
    once = redact_log_text(f"Authorization: Bearer abc.def.ghi and {_JWT} and a@b.co")
    assert redact_log_text(once) == once
    assert redact_log_text("") == ""


# ---- bounded input: redaction is linear and runs on a bounded window ------


def test_a_long_unbroken_run_does_not_blow_up() -> None:
    """The secret-key rules used to start with an unbounded `[\\w.-]*`, giving
    O(n^2) backtracking on one long token: 32k chars took 44s. Three call sites
    run this synchronously in the event loop on data-controlled text."""
    import time

    started = time.monotonic()
    out = redact_log_text("a" * 32_000)
    assert time.monotonic() - started < 2.0
    assert len(out) <= 32_000


def test_redact_bounded_cuts_to_the_limit() -> None:
    text = "keep this. " + "b" * 500
    out = redact_bounded(text, 40)
    assert len(out) <= 40
    assert out.startswith("keep this. ")


def test_redact_bounded_only_ever_sees_a_bounded_window() -> None:
    seen: list[int] = []

    def _spy(text: str) -> str:
        seen.append(len(text))
        return text

    redact_bounded("x" * 1_000_000, 100, redact=_spy)
    assert seen and max(seen) <= 100 + REDACT_OVERLAP_CHARS


def test_a_secret_straddling_the_cut_is_not_left_half_visible() -> None:
    """Cutting first and redacting after would hand the redactor a fragment it
    might not match. The window carries an overlap so the whole token is seen."""
    secret = "ghp_" + "a1b2c3d4e5" * 4
    out = redact_bounded("x" * 95 + " " + secret + " trailing", 100)
    assert secret not in out
    assert "ghp_a1b2" not in out
    assert len(out) <= 100


def test_redact_bounded_passes_short_text_through() -> None:
    assert redact_bounded("plain line", 100) == "plain line"
    assert redact_bounded("", 100) == ""
