"""Secrets encryption provider — Protocol + Fernet implementation.

FernetSecretsProvider derives a Fernet key from an arbitrary password string using
PBKDF2-HMAC-SHA256, allowing the caller to use a plain secret_key without worrying
about Fernet's exact 32-byte URL-safe base64 key format.

Usage::

    provider = FernetSecretsProvider(secret_key="my-secret-password")
    ciphertext = await provider.encrypt("api-key-value")
    plaintext  = await provider.decrypt(ciphertext)
"""

import base64
import hashlib
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import structlog
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = structlog.get_logger(__name__)

# Salt is fixed/public — security comes from the secret_key, not the salt.
# PBKDF2 is used only to expand/normalize the key to the 32-byte size Fernet needs.
# Deliberately not renamed: changing this value rotates all stored secrets.
_PBKDF2_SALT = b"legion-fernet-v1"
_PBKDF2_ITERATIONS = 390_000  # OWASP-recommended minimum for PBKDF2-HMAC-SHA256


@runtime_checkable
class SecretsProvider(Protocol):
    """Protocol for encrypting and decrypting secret strings at rest."""

    async def encrypt(self, plaintext: str) -> str:
        """Encrypt *plaintext* and return an opaque ciphertext string."""
        ...

    async def decrypt(self, ciphertext: str) -> str:
        """Decrypt *ciphertext* and return the original plaintext.

        Raises:
            cryptography.fernet.InvalidToken: if the ciphertext is invalid or the
                wrong key is used.
        """
        ...


#: The example ENVIRONMENT_SECRET_KEY shipped in .env.example; never a real key.
INSECURE_EXAMPLE_SECRET_KEY = "embry0-dev-secret-key"


class FernetSecretsProvider:
    """Fernet-backed SecretsProvider with a MultiFernet rotation path (RAV-1070).

    The *secret_key* is an arbitrary UTF-8 string.  It is deterministically
    stretched to a 32-byte URL-safe base64 key via PBKDF2-HMAC-SHA256 so that
    Fernet can accept it.

    *old_secret_keys* holds previously-active keys, newest first. ``encrypt()``
    always uses *secret_key*; ``decrypt()`` tries *secret_key* first, then each
    old key in order (Fernet tokens are HMAC-authenticated, so trying keys is
    exact — no explicit ciphertext version tag is needed). Rotation procedure:
    move the current key into ENVIRONMENT_SECRET_KEYS_OLD, set a fresh
    ENVIRONMENT_SECRET_KEY, restart; secrets re-encrypt on next write.

    Fernet tokens are URL-safe base64-encoded and include a random 128-bit IV, so
    two calls to ``encrypt()`` with the same plaintext produce different ciphertexts.
    """

    def __init__(self, secret_key: str, old_secret_keys: Sequence[str] = ()) -> None:
        self._fernet = MultiFernet([self._derive(k) for k in (secret_key, *old_secret_keys)])

    @staticmethod
    def _derive(secret_key: str) -> Fernet:
        derived = hashlib.pbkdf2_hmac(
            hash_name="sha256",
            password=secret_key.encode(),
            salt=_PBKDF2_SALT,
            iterations=_PBKDF2_ITERATIONS,
            dklen=32,
        )
        return Fernet(base64.urlsafe_b64encode(derived))

    async def encrypt(self, plaintext: str) -> str:
        """Encrypt *plaintext* under the primary key; return a Fernet token as a str."""
        token: bytes = self._fernet.encrypt(plaintext.encode())
        return token.decode()

    async def decrypt(self, ciphertext: str) -> str:
        """Decrypt a Fernet token and return the original plaintext.

        Raises:
            cryptography.fernet.InvalidToken: on bad token or wrong key.
        """
        try:
            plaintext_bytes: bytes = self._fernet.decrypt(ciphertext.encode())
        except InvalidToken:
            raise
        return plaintext_bytes.decode()


async def encrypt_vars(variables: list[dict[str, Any]], provider: SecretsProvider) -> list[dict[str, Any]]:
    """Encrypt the ``value`` of every ``var_type == 'secret'`` row; others pass through."""
    out: list[dict[str, Any]] = []
    for v in variables:
        d = dict(v)
        if d.get("var_type") == "secret":
            d["value"] = await provider.encrypt(d["value"])
        out.append(d)
    return out


async def decrypt_vars(variables: list[dict[str, Any]], provider: SecretsProvider) -> list[dict[str, Any]]:
    """Decrypt the ``value`` of every ``var_type == 'secret'`` row; others pass through.

    A row that fails to decrypt (rotated key, corrupt ciphertext) is kept with
    the sentinel value ``[DECRYPTION_FAILED]`` rather than aborting the batch.
    """
    out: list[dict[str, Any]] = []
    for v in variables:
        d = dict(v)
        if d.get("var_type") == "secret":
            try:
                d["value"] = await provider.decrypt(d["value"])
            except Exception:
                logger.warning("secret_decryption_failed", key=d["key"])
                d["value"] = "[DECRYPTION_FAILED]"
        out.append(d)
    return out
