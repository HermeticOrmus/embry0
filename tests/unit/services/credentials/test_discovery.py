from types import SimpleNamespace

import structlog

from embry0.services.credentials.discovery import forge_credential_entries


class FakeSecrets:
    async def decrypt(self, c):
        if c == "enc:BAD":
            from cryptography.fernet import InvalidToken

            raise InvalidToken()
        return c.removeprefix("enc:")


class FakeRepo:
    def __init__(self, rows):
        self.rows = {r["id"]: r for r in rows}

    async def list_all(self):
        return [{k: v for k, v in r.items() if k != "secret_ciphertext"} for r in self.rows.values()]

    async def get(self, cid):
        return self.rows.get(cid)


def _row(cid, label, provider="github", secret="tok", status="ok"):
    return {"id": cid, "label": label, "provider": provider, "status": status, "secret_ciphertext": "enc:" + secret}


async def test_registry_first_then_env_deduped_by_value(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN__ACME", "tok-acme")
    repo = FakeRepo(
        [
            _row("cred-1", "alice", secret="tok-alice"),
            _row("cred-2", "bob", secret="tok-acme"),
            _row("cred-3", "dis", secret="x", status="disabled"),
            _row("cred-4", "gl", provider="gitlab"),
        ]
    )
    cfg = SimpleNamespace(github_token="tok-default", gitlab_token="", gitlab_url="https://gitlab.example.com")
    entries = await forge_credential_entries(repo, FakeSecrets(), cfg, "github")
    assert [(e.key, e.credential_id, e.source) for e in entries] == [
        ("credential:alice", "cred-1", "registry"),
        ("credential:bob", "cred-2", "registry"),
        ("GITHUB_TOKEN", None, "env"),
    ]  # GITHUB_TOKEN__ACME dropped: same value as bob


async def test_undecryptable_row_skipped():
    repo = FakeRepo([_row("cred-1", "bad", secret="BAD"), _row("cred-2", "good")])
    cfg = SimpleNamespace(github_token="", gitlab_token="", gitlab_url="")
    entries = await forge_credential_entries(repo, FakeSecrets(), cfg, "github")
    assert [e.label for e in entries] == ["good"]


async def test_gitlab_env_keys(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN__GROUP", "tok-g")
    cfg = SimpleNamespace(github_token="", gitlab_token="tok-gl", gitlab_url="https://gitlab.example.com")
    entries = await forge_credential_entries(FakeRepo([]), FakeSecrets(), cfg, "gitlab")
    assert [e.key for e in entries] == ["GITLAB_TOKEN", "GITLAB_TOKEN__GROUP"]


def test_repr_and_response_copy_omit_the_token():
    from embry0.services.credentials.discovery import ForgeCredentialEntry

    entry = ForgeCredentialEntry("credential:alice", "cred-1", "alice", "ghp_alice_placeholder", "registry")
    assert "ghp_alice_placeholder" not in repr(entry)
    blank = entry.without_token()
    assert blank.token == ""
    assert (blank.key, blank.credential_id, blank.label, blank.source) == (
        "credential:alice",
        "cred-1",
        "alice",
        "registry",
    )
    assert entry.token == "ghp_alice_placeholder"


async def test_none_repo_returns_env_entries_without_a_registry_error(monkeypatch):
    """repo is None skips the registry. A swallowed AttributeError would still return env rows and log."""
    monkeypatch.setenv("GITHUB_TOKEN__ACME", "tok-acme")
    cfg = SimpleNamespace(github_token="tok-default", gitlab_token="", gitlab_url="")
    import embry0.services.credentials.discovery as discovery_mod

    discovery_mod.logger.__dict__.pop("bind", None)
    with structlog.testing.capture_logs() as captured:
        entries = await forge_credential_entries(None, None, cfg, "github")
    assert [(e.key, e.source) for e in entries] == [("GITHUB_TOKEN", "env"), ("GITHUB_TOKEN__ACME", "env")]
    assert not any(event.get("event") == "discovery_registry_error" for event in captured)
