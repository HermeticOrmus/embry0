"""CA-bundle loading and the sandbox trust env (spec 1A §3.2)."""

import ssl

import pytest

from embry0.execution.tls import (
    SANDBOX_CA_BUNDLE_PATH,
    CABundleError,
    build_ssl_context,
    load_ca_pem,
    sandbox_ca_env,
)

CA_PEM = """-----BEGIN CERTIFICATE-----
MIIDFTCCAf2gAwIBAgIUKGxb659gemN1Fu46hjP/cM50qQcwDQYJKoZIhvcNAQEL
BQAwGTEXMBUGA1UEAwwOZW1icnkwLXRlc3QtY2EwIBcNMjYwOTE4MTM1NTMxWhgP
MjEyNjA4MjUxMzU1MzFaMBkxFzAVBgNVBAMMDmVtYnJ5MC10ZXN0LWNhMIIBIjAN
BgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAps3bhaL+Zory+o1N1k2As7R5O2Ts
RAiM8Sy1gRgBWtZBoDSxsEiwj9y/mF3ktm1lIBCfC4DJC1s3l8y6Z2M6w8wU1fHJ
7FJcSyC8Xb5pW+DY16EmXs1N0Z/Sk8gaXKDxC/Eb2aWmbH82NO6lLkiXdlRLFM72
WJORS8RzLZWBSGJxegh+VzmisIFPe5pRyYw5aQ+79f4guvNhseKoN2/0vdsBEK1a
X/6zygGrh0jpeDaFmDj3mQI/N+ttg81rL5PRfwE+LCdSxgzVbmDSkyeslNDXJOOl
H6JbfgLbBb3eNOrCERngxcN0eO7GhHi5aSBASAHFs/EDnvX8cXnm3zvo/wIDAQAB
o1MwUTAdBgNVHQ4EFgQUK214K4DPd64gDs8w4/oQNCSX4V4wHwYDVR0jBBgwFoAU
K214K4DPd64gDs8w4/oQNCSX4V4wDwYDVR0TAQH/BAUwAwEB/zANBgkqhkiG9w0B
AQsFAAOCAQEAQ65bqCRpk1rsCUb55zrmLXKJwvAhfAoUvla6DDuyTX1IYLsFpSwE
sxBPs8f8vXhg703ospwDAaRGPljF0yEuBdh0XMK2cMFDgu2aY9f3/edr5MFaj8h6
R2LrznGBf7knKJr4MPKVhlUvNXPI0g90j2ofSxQ56GHC5ozk+/6JUF3bsOgqLrle
qoRdavJt88e/4KcKCra5RIwipuNVmlCeTYxVwCCSXlccESJ39bjHD2vDDqv2ro/E
c4e0+qw4L6q4kPNiZiNmumxWekny53FXSVrhhHJS4MwcuLyfvwBD4LsvuNhKYpgm
mJ6PoFKMXR9xjyWUTmkVT6oYfPdl0vzcQw==
-----END CERTIFICATE-----
"""

GARBAGE_PEM = "-----BEGIN CERTIFICATE-----\nnot base64 at all\n-----END CERTIFICATE-----\n"


def test_no_path_means_no_bundle() -> None:
    """An unset GITLAB_CA_BUNDLE is the normal case, not an error."""
    assert load_ca_pem("") == ""


def test_loads_a_real_pem(tmp_path) -> None:
    path = tmp_path / "ca.pem"
    path.write_text(CA_PEM)
    assert load_ca_pem(str(path)) == CA_PEM


def test_missing_file_names_the_path(tmp_path) -> None:
    missing = str(tmp_path / "nope.pem")
    with pytest.raises(CABundleError, match="nope.pem"):
        load_ca_pem(missing)


def test_non_certificate_file_is_refused(tmp_path) -> None:
    path = tmp_path / "ca.pem"
    path.write_text("just some text\n")
    with pytest.raises(CABundleError, match="no PEM certificate"):
        load_ca_pem(str(path))


def test_unparseable_certificate_is_refused(tmp_path) -> None:
    """A PEM header is not proof of a certificate — it must actually parse."""
    path = tmp_path / "ca.pem"
    path.write_text(GARBAGE_PEM)
    with pytest.raises(CABundleError, match="not a valid certificate"):
        load_ca_pem(str(path))


def test_non_utf8_file_is_refused(tmp_path) -> None:
    """A DER bundle is bytes, not text — it must still fail as a CABundleError."""
    path = tmp_path / "ca.der"
    path.write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(CABundleError, match="ca.der"):
        load_ca_pem(str(path))


def test_context_keeps_the_system_store() -> None:
    """GIT_SSL_CAINFO replaces the trust store; public hosts must still work."""
    system = len(ssl.create_default_context().get_ca_certs())
    context = build_ssl_context(CA_PEM)
    assert len(context.get_ca_certs()) == system + 1
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_empty_pem_yields_the_plain_system_context() -> None:
    system = len(ssl.create_default_context().get_ca_certs())
    assert len(build_ssl_context("").get_ca_certs()) == system


def test_sandbox_env_points_every_client_at_one_file() -> None:
    env = sandbox_ca_env()
    assert set(env) == {
        "GIT_SSL_CAINFO",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
    assert set(env.values()) == {SANDBOX_CA_BUNDLE_PATH}
    assert SANDBOX_CA_BUNDLE_PATH == "/home/agent/.embry0/ca-bundle.pem"
