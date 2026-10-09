"""What an xAI 403 means, read from its body.

xAI answers unrelated conditions with HTTP 403, and only the body tells them
apart. Read as one "credentials refused", each quarantined the grok account
with no end, so a spent spending cap or a stale access token stopped every
grok job until an operator cleared it.

- ``SPENDING_LIMIT``: the account's spending cap is spent. It lifts on its own.
- ``NO_SUBSCRIPTION``: the account has no SuperGrok subscription. Only a
  person can fix that.
- ``STALE_TOKEN``: xAI could not validate the access token. A refresh fixes it.

The phrases are the ones NousResearch/hermes-agent keys on for the same
conditions (``agent/error_classifier.py``, ``agent/agent_runtime_helpers.py``).
embry0 has not yet received any of them. They are matched case-insensitively
anywhere in the error text, so they hold whether xAI puts them in a ``code``
field or in an Anthropic-style ``error.message``.

Standard library only: the xai-proxy image copies this file on its own.
"""

from __future__ import annotations

from enum import Enum


class XaiDenial(Enum):
    SPENDING_LIMIT = "spending_limit"
    NO_SUBSCRIPTION = "no_subscription"
    STALE_TOKEN = "stale_token"


_SPENDING_LIMIT = ("personal-team-blocked:spending-limit", "spending limit", "spending-limit")
_NO_SUBSCRIPTION = ("do not have an active grok subscription",)
# "out of available resources" alone is generic; with "grok" beside it, it is
# xAI's wording for an account the grok entitlement does not cover.
_NO_RESOURCES = "out of available resources"
_STALE_TOKEN = ("[wke=unauthenticated:", "access token could not be validated")

# What the run's error says, ahead of xAI's own words.
DESCRIPTIONS: dict[XaiDenial, str] = {
    XaiDenial.SPENDING_LIMIT: "The xAI account has reached its spending limit (HTTP 403).",
    XaiDenial.NO_SUBSCRIPTION: (
        "The xAI account has no active SuperGrok subscription (HTTP 403). Renew it, then set the credential back to ok."
    ),
    XaiDenial.STALE_TOKEN: "xAI called the access token stale (HTTP 403); embry0 refreshes it.",
}


def classify_xai_denial(text: str | None) -> XaiDenial | None:
    """The condition an xAI 403's error text names, or None when it names none of them."""
    if not text:
        return None
    lowered = text.lower()
    if any(phrase in lowered for phrase in _SPENDING_LIMIT):
        return XaiDenial.SPENDING_LIMIT
    if any(phrase in lowered for phrase in _NO_SUBSCRIPTION) or (_NO_RESOURCES in lowered and "grok" in lowered):
        return XaiDenial.NO_SUBSCRIPTION
    if any(phrase in lowered for phrase in _STALE_TOKEN):
        return XaiDenial.STALE_TOKEN
    return None
