"""TLS trust material for a self-managed forge behind a private CA (spec 1A §3.2).

A private CA breaks three independent clients at once: the orchestrator's
httpx calls, the gitlab-proxy's httpx calls, and git inside every sandbox.
All three are fed from this one module, reading one file named by
``GITLAB_CA_BUNDLE`` — so trust is configured once and cannot drift between
them.

Two decisions worth keeping:

**Additive, never replacing.** The context is the system store *plus* the
private CA. The same client still reaches github.com and api.anthropic.com.

**The sandbox gets a merged file, not the CA alone.** ``GIT_SSL_CAINFO``
*replaces* git's trust store rather than extending it, and the same sandbox
may fetch from public hosts — so the helper that writes
``SANDBOX_CA_BUNDLE_PATH`` concatenates the system bundle with the CA.
"""

from __future__ import annotations

import ssl

#: In-sandbox path of the merged bundle, written by
#: ``/usr/local/bin/embry0-trust-ca`` (infra/scripts/embry0-trust-ca).
SANDBOX_CA_BUNDLE_PATH = "/home/agent/.embry0/ca-bundle.pem"

_PEM_MARKER = "-----BEGIN CERTIFICATE-----"


class CABundleError(RuntimeError):
    """The configured CA bundle is missing, unreadable, or not a certificate."""


def build_ssl_context(ca_pem: str) -> ssl.SSLContext:
    """System trust store plus ``ca_pem`` (empty ``ca_pem`` → system store only)."""
    context = ssl.create_default_context()
    if ca_pem:
        context.load_verify_locations(cadata=ca_pem)
    return context


def load_ca_pem(path: str) -> str:
    """PEM text at ``path``; ``""`` when no bundle is configured.

    Raises :class:`CABundleError` naming the path when it cannot be read or
    does not parse as at least one certificate. The orchestrator refuses to
    boot on that rather than silently falling back to the public trust store
    and failing every clone later with an opaque handshake error.
    """
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            pem = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, not an OSError: a DER bundle
        # named here is bytes, and must still fail as a CABundleError.
        raise CABundleError(f"GITLAB_CA_BUNDLE {path!r} could not be read: {exc}") from exc
    if _PEM_MARKER not in pem:
        raise CABundleError(f"GITLAB_CA_BUNDLE {path!r} contains no PEM certificate")
    try:
        build_ssl_context(pem)
    except ssl.SSLError as exc:
        raise CABundleError(f"GITLAB_CA_BUNDLE {path!r} is not a valid certificate: {exc}") from exc
    return pem


def sandbox_ca_env() -> dict[str, str]:
    """Env pointing every in-sandbox TLS client at the merged bundle.

    git, python-requests, curl and node each read a different variable and
    none of them consults the others, so all five are set.
    """
    return {
        "GIT_SSL_CAINFO": SANDBOX_CA_BUNDLE_PATH,
        "SSL_CERT_FILE": SANDBOX_CA_BUNDLE_PATH,
        "REQUESTS_CA_BUNDLE": SANDBOX_CA_BUNDLE_PATH,
        "CURL_CA_BUNDLE": SANDBOX_CA_BUNDLE_PATH,
        "NODE_EXTRA_CA_CERTS": SANDBOX_CA_BUNDLE_PATH,
    }
