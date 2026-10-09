"""Secrets must not ride a test command's output into the database (#341 review).

The sandbox environment holds XAI_API_KEY, the Claude OAuth token and the
proxy bearer. A target command that echoes its environment — `env`, `set -x`,
a framework that dumps config on failure — would otherwise put them in
job_verifications.report and in a deliverable the API serves.
"""

from __future__ import annotations

import pytest

from embry0.workflows.verification.scrub import REDACTED, scrub_output


@pytest.mark.parametrize(
    "line",
    [
        "XAI_API_KEY=xai-abcdef0123456789abcdef0123456789",
        "ANTHROPIC_API_KEY=sk-ant-api03-AAAABBBBCCCCDDDD",
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-ZZZZYYYYXXXX",
        "GITHUB_TOKEN=ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        "PROXY_ADMIN_TOKEN=Hs8_v-QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ",
        '  declare -x GITLAB_TOKEN="glpat-AAAAAAAAAAAAAAAAAAAA"',
        "POSTGRES_PASSWORD: hunter2hunter2hunter2",
        "Authorization: Bearer sk-ant-oat01-ZZZZYYYYXXXX",
        "embry0_secret_key=abcdef0123456789",
    ],
)
def test_a_secret_never_survives(line: str) -> None:
    scrubbed = scrub_output(line)
    assert REDACTED in scrubbed
    for secret in ("xai-abcdef", "sk-ant", "ghp_0123", "glpat-", "hunter2", "Hs8_v-", "abcdef0123456789"):
        assert secret not in scrubbed


def test_a_bare_token_shaped_string_is_caught_even_without_a_key_name() -> None:
    assert "xai-abcdef0123456789abcdef0123456789" not in scrub_output(
        "connecting with xai-abcdef0123456789abcdef0123456789"
    )


@pytest.mark.parametrize(
    "line",
    [
        "FAILED tests/test_a.py::test_key - AssertionError: expected 3",
        "[ERROR]   LeadIdleTimerServiceImplTest.returnsToPool:42 expected: <1> but was: <2>",
        "==== 1 failed, 40 passed in 3.21s ====",
        "PATH=/usr/local/bin:/usr/bin",
        "NODE_ENV=test",
        "api_key_missing: the fixture did not set one",
        "Tests run: 212, Failures: 1, Errors: 1, Skipped: 0",
    ],
)
def test_ordinary_output_is_untouched(line: str) -> None:
    assert scrub_output(line) == line


def test_the_shape_of_the_output_is_preserved() -> None:
    text = "line one\nSECRET_TOKEN=abcdefghijklmnop\nline three"
    scrubbed = scrub_output(text)
    assert scrubbed.splitlines()[0] == "line one"
    assert scrubbed.splitlines()[2] == "line three"
    assert "abcdefghijklmnop" not in scrubbed


def test_empty_input_is_handled() -> None:
    assert scrub_output("") == ""


def test_known_values_are_redacted_verbatim_longest_first() -> None:
    """Spec 2026-09-29 §5: a registry secret has no known shape, so it is removed by value."""
    text = "echo s3cr3t-nonstandard-value-long and s3cr3t-nonstandard-value"
    scrubbed = scrub_output(text, known_values=["s3cr3t-nonstandard-value", "s3cr3t-nonstandard-value-long"])
    assert "s3cr3t" not in scrubbed, scrubbed
    assert scrubbed == f"echo {REDACTED} and {REDACTED}"
