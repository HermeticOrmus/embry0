from types import SimpleNamespace

from embry0.services.credentials.resolver import CredentialResolver


class FakeSecrets:
    async def decrypt(self, c):
        if c == "enc:BAD":
            from cryptography.fernet import InvalidToken

            raise InvalidToken()
        return c.removeprefix("enc:")


class FakeRepo:
    async def all_ciphertexts(self):
        return [
            {"id": "cred-1", "secret_ciphertext": "enc:longsecret1"},
            {"id": "cred-2", "secret_ciphertext": "enc:BAD"},
            {"id": "cred-3", "secret_ciphertext": "enc:short"},
        ]


class CountingRepo(FakeRepo):
    def __init__(self):
        self.get_calls = 0
        self.cipher_calls = 0

    async def list_all(self):
        return [{"id": "a"}, {"id": "b"}]

    async def get(self, cid):
        self.get_calls += 1
        return {"secret_ciphertext": f"enc:longenough{cid}"}

    async def all_ciphertexts(self):
        self.cipher_calls += 1
        return [
            {"id": "a", "secret_ciphertext": "enc:longenougha"},
            {"id": "b", "secret_ciphertext": "enc:longenoughb"},
        ]


async def test_known_secret_values_reads_ciphertexts_in_one_query():
    """#439: list_all plus get() per row re-reads every secret at startup and after each write."""
    repo = CountingRepo()
    r = CredentialResolver(
        repo, FakeSecrets(), SimpleNamespace(github_token="", gitlab_token="", gitlab_url=""), environ={}
    )
    assert await r.known_secret_values() == ["longenougha", "longenoughb"]
    assert repo.get_calls == 0, f"known_secret_values issued {repo.get_calls} get() calls"
    assert repo.cipher_calls == 1


async def test_known_secret_values_skips_bad_and_short_and_caches():
    r = CredentialResolver(
        FakeRepo(), FakeSecrets(), SimpleNamespace(github_token="", gitlab_token="", gitlab_url=""), environ={}
    )
    assert await r.known_secret_values() == ["longsecret1"]
    assert await r.known_secret_values() == ["longsecret1"]
    assert await r.known_secret_values(refresh=True) == ["longsecret1"]
