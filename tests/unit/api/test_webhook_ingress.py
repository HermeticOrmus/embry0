"""Shared webhook ingress — one verification policy across three forges (#159)."""

import json

import pytest
from fastapi import HTTPException

from embry0.api.webhook_ingress import (
    SignatureScheme,
    parse_webhook_body,
    unwrap_smee_envelope,
    verify_webhook,
)
from tests.fixtures.webhooks import github_issue, sign, smee_envelope

BODY = b'{"hello": "world"}'
SECRET = "s3cret"


def _verify(scheme: SignatureScheme, presented: str, secret: str = SECRET, dev: bool = False) -> None:
    verify_webhook(
        scheme=scheme,
        body=BODY,
        presented=presented,
        secret=secret,
        webhook_dev_mode=dev,
        forge="Test",
    )


# --- the three schemes ------------------------------------------------------


@pytest.mark.parametrize(
    "scheme",
    [
        SignatureScheme.HMAC_SHA256_PREFIXED,
        SignatureScheme.HMAC_SHA256_BARE,
        SignatureScheme.SHARED_SECRET,
    ],
)
def test_a_correctly_signed_delivery_passes(scheme):
    _verify(scheme, sign(scheme, BODY, SECRET))


def test_github_requires_the_sha256_prefix():
    """Bare hex is Linear's convention and must not authenticate as GitHub."""
    bare = sign(SignatureScheme.HMAC_SHA256_BARE, BODY, SECRET)
    with pytest.raises(HTTPException) as exc:
        _verify(SignatureScheme.HMAC_SHA256_PREFIXED, bare)
    assert exc.value.status_code == 401


def test_linear_rejects_the_github_prefix():
    prefixed = sign(SignatureScheme.HMAC_SHA256_PREFIXED, BODY, SECRET)
    with pytest.raises(HTTPException) as exc:
        _verify(SignatureScheme.HMAC_SHA256_BARE, prefixed)
    assert exc.value.status_code == 401


def test_gitlab_compares_the_token_verbatim():
    _verify(SignatureScheme.SHARED_SECRET, SECRET)
    with pytest.raises(HTTPException):
        _verify(SignatureScheme.SHARED_SECRET, "wrong")


def test_a_signature_over_a_different_body_fails():
    other = sign(SignatureScheme.HMAC_SHA256_PREFIXED, b"tampered", SECRET)
    with pytest.raises(HTTPException) as exc:
        _verify(SignatureScheme.HMAC_SHA256_PREFIXED, other)
    assert exc.value.status_code == 401


# --- the shared policy ------------------------------------------------------


@pytest.mark.parametrize("scheme", list(SignatureScheme))
def test_missing_secret_outside_dev_mode_is_503(scheme):
    """Silently accepting anything is worse than refusing to run."""
    with pytest.raises(HTTPException) as exc:
        _verify(scheme, "anything", secret="", dev=False)
    assert exc.value.status_code == 503


@pytest.mark.parametrize("scheme", list(SignatureScheme))
def test_missing_secret_in_dev_mode_is_allowed(scheme):
    _verify(scheme, "", secret="", dev=True)


@pytest.mark.parametrize("scheme", list(SignatureScheme))
def test_dev_mode_never_bypasses_a_configured_secret(scheme):
    """The load-bearing rule, now identical for every forge.

    Linear's inline copy checked dev mode *before* the secret, so a configured
    secret was ignored whenever WEBHOOK_DEV_MODE was on — contradicting its own
    docstring.
    """
    with pytest.raises(HTTPException) as exc:
        _verify(scheme, "wrong", secret=SECRET, dev=True)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("scheme", list(SignatureScheme))
def test_missing_header_with_a_configured_secret_is_401(scheme):
    with pytest.raises(HTTPException) as exc:
        _verify(scheme, "")
    assert exc.value.status_code == 401


def test_the_forge_name_reaches_the_error_detail():
    with pytest.raises(HTTPException) as exc:
        verify_webhook(
            scheme=SignatureScheme.SHARED_SECRET,
            body=BODY,
            presented="",
            secret="",
            webhook_dev_mode=False,
            forge="GitLab",
        )
    assert "GitLab" in exc.value.detail


# --- body parsing -----------------------------------------------------------


def test_direct_delivery_passes_through():
    payload = github_issue(action="labeled", labels=["embry0"])
    assert parse_webhook_body(json.dumps(payload).encode()) == payload


def test_smee_envelope_is_unwrapped():
    payload = github_issue()
    assert parse_webhook_body(json.dumps(smee_envelope(payload)).encode()) == payload


def test_unwrap_is_single_pass():
    """A real payload may legitimately carry a string field named `payload`.

    Recursing would corrupt it — this is the regression the original inline
    unwrap was already guarded against, preserved through the extraction.
    """
    inner = {"action": "opened", "payload": "a string that is not JSON"}
    assert unwrap_smee_envelope(smee_envelope(inner)) == inner


def test_non_envelope_payload_field_is_left_alone():
    payload = {"action": "opened", "payload": "not json at all"}
    assert unwrap_smee_envelope(payload) == payload


def test_invalid_json_is_400():
    with pytest.raises(HTTPException) as exc:
        parse_webhook_body(b"not json")
    assert exc.value.status_code == 400


def test_non_object_json_is_400():
    """A bare list or scalar is not a delivery."""
    with pytest.raises(HTTPException) as exc:
        parse_webhook_body(b"[1, 2, 3]")
    assert exc.value.status_code == 400
