"""Unit tests for FernetSecretsProvider."""

import pytest

from embry0.storage.encryption import FernetSecretsProvider


@pytest.mark.asyncio
async def test_fernet_round_trip():
    p = FernetSecretsProvider(secret_key="test-key-123")
    ct = await p.encrypt("my-secret-value")
    assert ct != "my-secret-value"
    assert await p.decrypt(ct) == "my-secret-value"


@pytest.mark.asyncio
async def test_fernet_wrong_key_raises():
    from cryptography.fernet import InvalidToken

    p1 = FernetSecretsProvider(secret_key="key-a")
    p2 = FernetSecretsProvider(secret_key="key-b")
    ct = await p1.encrypt("x")
    with pytest.raises(InvalidToken):
        await p2.decrypt(ct)


@pytest.mark.asyncio
async def test_fernet_deterministic_key_nondeterministic_ciphertext():
    """Same secret_key -> same Fernet key, but ciphertexts differ per call (IV randomness)."""
    p = FernetSecretsProvider(secret_key="same-key")
    a = await p.encrypt("hello")
    b = await p.encrypt("hello")
    assert a != b
    assert await p.decrypt(a) == "hello"
    assert await p.decrypt(b) == "hello"


# ---- RAV-1070: MultiFernet key rotation ----


@pytest.mark.asyncio
async def test_rotated_key_still_decrypts_old_ciphertext():
    old = FernetSecretsProvider(secret_key="key-v1")
    ct = await old.encrypt("survive-rotation")
    rotated = FernetSecretsProvider(secret_key="key-v2", old_secret_keys=["key-v1"])
    assert await rotated.decrypt(ct) == "survive-rotation"


@pytest.mark.asyncio
async def test_rotated_provider_encrypts_under_primary_key():
    """New ciphertexts must use the NEW key — a provider with only the new key reads them."""
    rotated = FernetSecretsProvider(secret_key="key-v2", old_secret_keys=["key-v1"])
    ct = await rotated.encrypt("fresh")
    new_only = FernetSecretsProvider(secret_key="key-v2")
    assert await new_only.decrypt(ct) == "fresh"
    old_only = FernetSecretsProvider(secret_key="key-v1")
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        await old_only.decrypt(ct)


@pytest.mark.asyncio
async def test_multiple_old_keys_all_decrypt():
    v1 = FernetSecretsProvider(secret_key="key-v1")
    v2 = FernetSecretsProvider(secret_key="key-v2")
    ct1, ct2 = await v1.encrypt("a"), await v2.encrypt("b")
    rotated = FernetSecretsProvider(secret_key="key-v3", old_secret_keys=["key-v2", "key-v1"])
    assert await rotated.decrypt(ct1) == "a"
    assert await rotated.decrypt(ct2) == "b"


@pytest.mark.asyncio
async def test_unknown_key_still_raises_after_rotation():
    stranger = FernetSecretsProvider(secret_key="unrelated")
    ct = await stranger.encrypt("x")
    rotated = FernetSecretsProvider(secret_key="key-v2", old_secret_keys=["key-v1"])
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        await rotated.decrypt(ct)


def test_config_old_keys_list_parsing():
    from embry0.config import Embry0Config

    cfg = Embry0Config(environment_secret_keys_old=" key-v2 , key-v1 ,, ")
    assert cfg.environment_secret_keys_old_list == ["key-v2", "key-v1"]
    assert Embry0Config().environment_secret_keys_old_list == []


# ---- RAV-1064: var-list helpers moved here from the environment API route ----


@pytest.mark.asyncio
async def test_encrypt_vars_encrypts_only_secret_rows():
    from embry0.storage.encryption import encrypt_vars

    p = FernetSecretsProvider(secret_key="k")
    rows = [
        {"key": "API_KEY", "value": "s3cret", "var_type": "secret"},
        {"key": "LOG_LEVEL", "value": "debug", "var_type": "config"},
    ]
    out = await encrypt_vars(rows, p)
    assert out[0]["value"] != "s3cret"
    assert await p.decrypt(out[0]["value"]) == "s3cret"
    assert out[1]["value"] == "debug"
    # Input rows untouched (helpers copy).
    assert rows[0]["value"] == "s3cret"


@pytest.mark.asyncio
async def test_decrypt_vars_round_trip_and_failure_sentinel():
    from embry0.storage.encryption import decrypt_vars, encrypt_vars

    p = FernetSecretsProvider(secret_key="k")
    encrypted = await encrypt_vars([{"key": "API_KEY", "value": "s3cret", "var_type": "secret"}], p)
    ok = await decrypt_vars(encrypted, p)
    assert ok[0]["value"] == "s3cret"

    wrong = FernetSecretsProvider(secret_key="other")
    failed = await decrypt_vars(encrypted, wrong)
    assert failed[0]["value"] == "[DECRYPTION_FAILED]"
