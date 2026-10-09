"""CredentialsRepository — spec 2026-09-29 §4 validation rules and the use ledger."""

import pytest

from embry0.services.credentials.resolver import CredentialResolver
from embry0.storage.database import DatabasePool
from embry0.storage.repositories.credentials import (
    BindingValidationError,
    CredentialBoundError,
    CredentialsRepository,
    new_credential_id,
)

pytestmark = [pytest.mark.requires_postgres]


async def _gh(repo: CredentialsRepository, label: str, **kw) -> dict:
    return await repo.create(
        kind="forge_token",
        provider="github",
        host="github.com",
        label=label,
        secret_ciphertext="enc:" + label,
        masked_preview="ghp_****abcd",
        created_by="test",
        **kw,
    )


def test_new_credential_id_shape() -> None:
    cid = new_credential_id()
    assert cid.startswith("cred-") and len(cid) == 17


@pytest.mark.parametrize("value", ["github:a:b", "gitlab:acme:x"])
async def test_owner_scope_with_a_colon_in_the_owner_is_refused(value: str) -> None:
    """The resolver looks up '<forge>:<owner>' with an owner that never holds ':', so
    'github:a:b' could never match. Refused before any DB call, hence no Postgres."""
    from unittest.mock import MagicMock

    repo = CredentialsRepository(MagicMock())
    with pytest.raises(BindingValidationError, match="owner scope_value"):
        await repo.replace_bindings("owner", value, [], created_by="test")


async def test_create_get_list_hide_ciphertext_in_list(db_with_migrations: DatabasePool) -> None:
    repo = CredentialsRepository(db_with_migrations)
    row = await _gh(repo, "alice", git_author_name="Alice", git_author_email="alice@example.com")
    assert row["id"].startswith("cred-") and row["status"] == "ok"
    got = await repo.get(row["id"])
    assert got["secret_ciphertext"] == "enc:alice"
    listed = await repo.list_all()
    assert listed[0]["label"] == "alice" and "secret_ciphertext" not in listed[0]
    assert listed[0]["binding_count"] == 0


async def test_label_unique_per_provider(db_with_migrations: DatabasePool) -> None:
    import asyncpg

    repo = CredentialsRepository(db_with_migrations)
    await _gh(repo, "alice")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _gh(repo, "alice")


async def test_replace_bindings_validates_role_kind_and_scope(db_with_migrations: DatabasePool) -> None:
    repo = CredentialsRepository(db_with_migrations)
    gh = await _gh(repo, "alice")
    with pytest.raises(BindingValidationError, match="role 'llm' needs"):
        await repo.replace_bindings(
            "repo", "acme/widgets", [{"credential_id": gh["id"], "role": "llm"}], created_by="t"
        )
    # llm is not bindable at host scope; the scope check fires before the kind check.
    with pytest.raises(BindingValidationError, match="scope 'host' is not valid for role 'llm'"):
        await repo.replace_bindings("host", "github.com", [{"credential_id": gh["id"], "role": "llm"}], created_by="t")
    with pytest.raises(BindingValidationError, match="pinned"):
        await repo.replace_bindings(
            "repo", "acme/widgets", [{"credential_id": gh["id"], "role": "forge", "pinned": True}], created_by="t"
        )
    with pytest.raises(BindingValidationError, match="forge 'gitlab'"):
        await repo.replace_bindings(
            "repo", "acme/widgets", [{"credential_id": gh["id"], "role": "forge"}], created_by="t", repo_forge="gitlab"
        )
    assert await repo.bindings_for_scope("repo", "acme/widgets") == []


async def test_replace_bindings_writes_and_candidates_order(db_with_migrations: DatabasePool) -> None:
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    b = await _gh(repo, "bob")
    await repo.replace_bindings("owner", "github:acme", [{"credential_id": a["id"], "role": "forge"}], created_by="t")
    rows = await repo.candidates(scope_kind="owner", scope_value="github:acme", role="forge", provider="github")
    assert [r["label"] for r in rows] == ["alice"] and rows[0]["secret_ciphertext"] == "enc:alice"
    await repo.replace_bindings("owner", "github:acme", [{"credential_id": b["id"], "role": "forge"}], created_by="t")
    rows = await repo.candidates(scope_kind="owner", scope_value="github:acme", role="forge", provider="github")
    assert [r["label"] for r in rows] == ["bob"]
    listed = await repo.list_all()
    assert [(r["label"], r["binding_count"]) for r in listed] == [("alice", 0), ("bob", 1)]


async def _llm(repo: CredentialsRepository, label: str) -> dict:
    return await repo.create(
        kind="llm_api_key",
        provider="anthropic",
        host="api.anthropic.com",
        label=label,
        secret_ciphertext="enc:" + label,
        masked_preview="sk-****abcd",
        created_by="test",
    )


async def test_candidates_order_by_priority(db_with_migrations: DatabasePool) -> None:
    repo = CredentialsRepository(db_with_migrations)
    a, b, c = [await _llm(repo, n) for n in ("a", "b", "c")]
    await repo.replace_bindings(
        "global",
        "",
        [
            {"credential_id": a["id"], "role": "llm", "priority": 2},
            {"credential_id": b["id"], "role": "llm", "priority": 0},
            {"credential_id": c["id"], "role": "llm", "priority": 1},
        ],
        created_by="t",
    )
    rows = await repo.candidates(scope_kind="global", scope_value="", role="llm", provider="anthropic")
    assert [r["label"] for r in rows] == ["b", "c", "a"]


async def test_candidates_equal_priority_never_used_first(db_with_migrations: DatabasePool) -> None:
    repo = CredentialsRepository(db_with_migrations)
    a = await _llm(repo, "a")
    b = await _llm(repo, "b")
    await repo.replace_bindings(
        "global",
        "",
        [{"credential_id": a["id"], "role": "llm"}, {"credential_id": b["id"], "role": "llm"}],
        created_by="t",
    )
    # a was bound first (lower binding id); using it must push it behind the never-used b.
    await repo.record_use(credential_id=a["id"], credential_label="a", job_id="job-o", role="llm", provider="anthropic")
    rows = await repo.candidates(scope_kind="global", scope_value="", role="llm", provider="anthropic")
    assert [r["label"] for r in rows] == ["b", "a"]


async def test_replace_bindings_rejects_duplicate_and_bad_types(db_with_migrations: DatabasePool) -> None:
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    b = await _gh(repo, "bob")
    x = await _llm(repo, "x")
    with pytest.raises(BindingValidationError, match="only one 'forge' credential"):
        await repo.replace_bindings(
            "repo",
            "acme/widgets",
            [{"credential_id": a["id"], "role": "forge"}, {"credential_id": b["id"], "role": "forge"}],
            created_by="t",
        )
    with pytest.raises(BindingValidationError, match="pinned must be a boolean"):
        await repo.replace_bindings(
            "repo", "acme/widgets", [{"credential_id": x["id"], "role": "llm", "pinned": "false"}], created_by="t"
        )
    assert await repo.bindings_for_scope("repo", "acme/widgets") == []
    rows = await repo.replace_bindings(
        "repo", "acme/widgets", [{"credential_id": a["id"], "role": "forge", "priority": None}], created_by="t"
    )
    assert [(r["label"], r["priority"]) for r in rows] == [("alice", 0)]


async def test_delete_maps_fk_race_to_bound_error(db_with_migrations: DatabasePool) -> None:
    """A binding created between the count and the DELETE still raises CredentialBoundError."""
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    real_fetchrow = db_with_migrations.fetchrow

    async def count_says_unbound(query: str, *args):
        if "COUNT(*) AS n FROM credential_bindings" in query:
            await repo.replace_bindings("global", "", [{"credential_id": a["id"], "role": "forge"}], created_by="t")
            return {"n": 0}
        return await real_fetchrow(query, *args)

    db_with_migrations.fetchrow = count_says_unbound  # type: ignore[method-assign]
    try:
        with pytest.raises(CredentialBoundError):
            await repo.delete(a["id"])
    finally:
        db_with_migrations.fetchrow = real_fetchrow  # type: ignore[method-assign]


async def test_delete_refused_while_bound(db_with_migrations: DatabasePool) -> None:
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    await repo.replace_bindings("global", "", [{"credential_id": a["id"], "role": "forge"}], created_by="t")
    with pytest.raises(CredentialBoundError):
        await repo.delete(a["id"])
    await repo.replace_bindings("global", "", [], created_by="t")
    assert await repo.delete(a["id"]) is True
    assert await repo.get(a["id"]) is None


async def test_delete_keeps_use_history_label(db_with_migrations: DatabasePool) -> None:
    """Review focus 5: history survives deletion with the label intact."""
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    await repo.record_use(
        credential_id=a["id"], credential_label="alice", job_id="job-1", role="forge", provider="github"
    )
    assert (await repo.get(a["id"]))["last_used_at"] is not None
    assert await repo.delete(a["id"]) is True
    uses = await repo.uses_for_job("job-1")
    assert uses[0]["credential_id"] is None and uses[0]["credential_label"] == "alice"


async def test_finish_uses_and_status(db_with_migrations: DatabasePool) -> None:
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    await repo.record_use(
        credential_id=a["id"], credential_label="alice", job_id="job-2", role="forge", provider="github"
    )
    assert await repo.finish_uses("job-2", "ok") == 1
    assert (await repo.uses_for_job("job-2"))[0]["outcome"] == "ok"
    row = await repo.set_status(a["id"], "disabled", reason="decryption_failed")
    assert row["status"] == "disabled" and row["status_reason"] == "decryption_failed"
    assert await repo.any_for_provider("github") is False
    await repo.set_status(a["id"], "ok")
    assert await repo.any_for_provider("github") is True
    assert await repo.any_for_provider("gitlab") is False


async def _use(repo: CredentialsRepository, cred: dict, job_id: str) -> None:
    await repo.record_use(
        credential_id=cred["id"], credential_label=cred["label"], job_id=job_id, role="forge", provider="github"
    )


async def test_finish_uses_covers_derived_sandbox_ids(db_with_migrations: DatabasePool) -> None:
    """QA/onboarding sandboxes run under ``<job_id>__<suffix>``; the parent's stamp and read must cover them."""
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    for jid in ("job-1", "job-1__bootstrap", "job-1__app_a", "job-10", "job-100", "job-2__x"):
        await _use(repo, a, jid)
    assert await repo.finish_uses("job-1", "ok") == 3, "parent stamp must cover exactly job-1 and its __ ids"
    uses = await repo.uses_for_job("job-1")
    assert [u["job_id"] for u in uses] == ["job-1", "job-1__bootstrap", "job-1__app_a"]
    assert all(u["outcome"] == "ok" for u in uses)
    for other in ("job-10", "job-100", "job-2__x"):
        rows = await repo.uses_for_job(other)
        assert [u["outcome"] for u in rows] == [None], f"{other} must stay unstamped"


async def test_finish_uses_escapes_like_wildcards_in_job_id(db_with_migrations: DatabasePool) -> None:
    """``_`` and ``%`` in a job id are literal, so ``job_a`` never reaches ``job-a__x`` or ``job_b``."""
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    for jid in ("job_a", "job_a__qa", "job-a__x", "job_b", "job%", "jobz__y"):
        await _use(repo, a, jid)
    assert await repo.finish_uses("job_a", "ok") == 2, "job_a must stamp only job_a and job_a__qa"
    assert [u["job_id"] for u in await repo.uses_for_job("job_a")] == ["job_a", "job_a__qa"]
    assert await repo.finish_uses("job%", "failed") == 1, "job% must not match jobz__y"
    assert [u["job_id"] for u in await repo.uses_for_job("job%")] == ["job%"]
    for other in ("job-a__x", "job_b", "jobz__y"):
        assert [u["outcome"] for u in await repo.uses_for_job(other)] == [None], f"{other} must stay unstamped"


async def test_rotate_reenables_only_decryption_failed(db_with_migrations: DatabasePool) -> None:
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    b = await _gh(repo, "bob")
    await repo.set_status(a["id"], "disabled", reason="decryption_failed")
    await repo.set_status(b["id"], "disabled", reason="revoked")
    ra = await repo.rotate(a["id"], secret_ciphertext="enc:a2", masked_preview="ghp_****a2a2")
    rb = await repo.rotate(b["id"], secret_ciphertext="enc:b2", masked_preview="ghp_****b2b2")
    assert (ra["status"], ra["status_reason"]) == ("ok", None)
    assert (rb["status"], rb["status_reason"]) == ("disabled", "revoked")


async def test_rotate_and_update_meta(db_with_migrations: DatabasePool) -> None:
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    r = await repo.rotate(a["id"], secret_ciphertext="enc:new", masked_preview="ghp_****zzzz")
    assert r["rotated_at"] is not None and (await repo.get(a["id"]))["secret_ciphertext"] == "enc:new"
    m = await repo.update_meta(a["id"], label="alice-2", git_author_email="a2@example.com")
    assert m["label"] == "alice-2" and m["git_author_email"] == "a2@example.com"
    assert await repo.find_by_label("github", "alice-2") is not None


async def test_scope_values_normalize_like_the_resolver_lookup(db_with_migrations: DatabasePool) -> None:
    """A mixed-case write is stored in the form CredentialResolver looks up."""
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    await repo.replace_bindings("repo", "Acme/Widgets", [{"credential_id": a["id"], "role": "forge"}], created_by="t")
    await repo.replace_bindings(
        "owner", "github:My-Org.X", [{"credential_id": a["id"], "role": "forge"}], created_by="t"
    )
    await repo.replace_bindings("host", "GitHub.COM", [{"credential_id": a["id"], "role": "forge"}], created_by="t")
    owner_lookup = "github:" + CredentialResolver.owner_of("My-Org.X/widgets")
    for kind, lookup in (("repo", "acme/widgets"), ("owner", owner_lookup), ("host", "github.com")):
        rows = await repo.candidates(scope_kind=kind, scope_value=lookup, role="forge", provider="github")
        assert [r["label"] for r in rows] == ["alice"], f"{kind} binding not found under normalized {lookup!r}"
        stored = await repo.bindings_for_scope(kind, lookup)
        assert [b["scope_value"] for b in stored] == [lookup], f"{kind} stored as {stored!r}"
    # Reading back with the caller's original spelling finds the same row.
    assert [b["label"] for b in await repo.bindings_for_scope("owner", "github:My-Org.X")] == ["alice"]
    assert [b["label"] for b in await repo.bindings_for_scope("repo", "Acme/Widgets")] == ["alice"]


async def test_owner_scope_needs_a_forge_prefix_matching_the_credential(db_with_migrations: DatabasePool) -> None:
    """The resolver only ever asks for '<forge>:<owner>' with a credential of that forge."""
    repo = CredentialsRepository(db_with_migrations)
    a = await _gh(repo, "alice")
    with pytest.raises(BindingValidationError, match="github:"):
        await repo.replace_bindings("owner", "acme-corp", [{"credential_id": a["id"], "role": "forge"}], created_by="t")
    with pytest.raises(BindingValidationError, match="gitlab"):
        await repo.replace_bindings(
            "owner", "gitlab:acme", [{"credential_id": a["id"], "role": "forge"}], created_by="t"
        )
    assert await repo.bindings_for_scope("owner", "gitlab:acme") == []
