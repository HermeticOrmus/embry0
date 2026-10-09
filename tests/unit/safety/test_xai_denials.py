"""What an xAI 403 means (embry0/safety/xai_denials.py).

xAI answers a spent spending cap, a missing subscription and a stale access
token all with HTTP 403. Read as one "credentials refused", each quarantined
the grok account with no end. The bodies below follow the phrases
NousResearch/hermes-agent keys on; embry0 has not yet seen a real one, so each
is matched case-insensitively anywhere in the error text.
"""

from __future__ import annotations

import pytest

from embry0.safety.error_codes import ErrorCode, classify_agent_error
from embry0.safety.xai_denials import XaiDenial, classify_xai_denial

SPENDING = "Error code: 403 - {'code': 'personal-team-blocked:spending-limit', 'error': 'Team spending limit reached'}"
STALE = "Error code: 403 - {'error': 'The OAuth2 access token could not be validated [WKE=unauthenticated:expired]'}"
NO_SUB = "Error code: 403 - {'error': 'You do not have an active Grok subscription.'}"
NO_RESOURCES = "Error code: 403 - {'error': 'Grok: you are out of available resources for this plan'}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (SPENDING, XaiDenial.SPENDING_LIMIT),
        (STALE, XaiDenial.STALE_TOKEN),
        ("Error code: 403 - {'error': 'denied [wke=UNAUTHENTICATED:token]'}", XaiDenial.STALE_TOKEN),
        (NO_SUB, XaiDenial.NO_SUBSCRIPTION),
        (NO_RESOURCES, XaiDenial.NO_SUBSCRIPTION),
    ],
)
def test_each_403_body_is_told_apart(text, expected):
    assert classify_xai_denial(text) is expected, f"{text!r} must read as {expected}"


@pytest.mark.parametrize(
    "text",
    [
        "Error code: 403 - {'error': 'Forbidden'}",
        # "out of available resources" alone is not xAI's grok entitlement wording.
        "Error code: 403 - {'error': 'out of available resources'}",
        "",
    ],
)
def test_an_unrecognised_403_is_not_guessed(text):
    assert classify_xai_denial(text) is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (SPENDING, ErrorCode.PROVIDER_QUOTA_EXHAUSTED),
        (STALE, ErrorCode.PROVIDER_TOKEN_STALE),
        (NO_SUB, ErrorCode.AUTH_REJECTED),
        ("Error code: 403 - {'error': 'Forbidden'}", ErrorCode.AUTH_REJECTED),
    ],
)
def test_the_text_classifier_reads_xai_403_bodies(text, code):
    """The CLI executor's failures reach the code through this text path, not the typed one."""
    assert classify_agent_error(text) is code, f"{text!r} must classify as {code}"
