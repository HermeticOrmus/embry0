import pytest

from embry0.safety import redaction

# These tests never read the real environment (environ={}) and never assert on
# a value list directly: a failure must not print local secrets.


@pytest.fixture(autouse=True)
def _empty_registry():
    redaction.register_known_secrets([], reset=True)
    yield
    redaction.register_known_secrets([], reset=True)


def test_registered_secret_is_scrubbed_even_when_not_in_env():
    redaction.register_known_secrets(["ghp_registryonly123"])
    values = redaction.secret_env_values(environ={})
    found = "ghp_registryonly123" in values
    assert found is True
    out = redaction.redact_secrets("push failed for ghp_registryonly123 at origin", values)
    assert "ghp_registryonly123" not in out


def test_short_values_are_not_registered():
    redaction.register_known_secrets(["abc"], reset=True)
    registered = "abc" in redaction.known_secret_values()
    assert registered is False
