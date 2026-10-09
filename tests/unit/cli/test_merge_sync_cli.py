"""embry0 merge-sync check|backfill (#279)."""

import os
from datetime import timedelta

import pytest

from embry0.cli import merge_sync
from embry0.config import Embry0Config
from tests.unit.services.merge_sync.conftest import REPO, T0, Seed, pr_object
from tests.unit.services.merge_sync.test_sweep import FakeGitHub

URL = f"https://github.com/{REPO}/pull/1"


def _report(**over):
    base = {
        "checked": 1,
        "requests": 1,
        "dry_run": True,
        "changes": [],
        "unverifiable": [],
        "errors": 0,
        "stopped": None,
    }
    return {**base, **over}


CHANGE = {
    "pr_url": URL,
    "job_id": "job-1",
    "matched_by": "pr_url",
    "delivery_from": "pr_open",
    "delivery_to": "merged",
    "status_from": "failed",
    "status_to": "failed",
    "pr_url_linked": False,
}


def test_exit_codes_say_whether_the_db_equals_github():
    assert merge_sync.exit_code("check", _report()) == 0
    assert merge_sync.exit_code("check", _report(changes=[CHANGE])) == 1
    assert merge_sync.exit_code("backfill", _report(changes=[CHANGE], dry_run=False)) == 0
    assert merge_sync.exit_code("check", _report(unverifiable=[{"pr_url": URL, "reason": "http_404"}])) == 3
    assert merge_sync.exit_code("backfill", _report(stopped="rate_limit_floor")) == 3


def test_render_names_every_job_and_pr_that_changes():
    text = merge_sync.render("check", _report(changes=[CHANGE], unverifiable=[{"pr_url": "x", "reason": "http_404"}]))
    assert f"would change: job-1  {URL}  delivery pr_open -> merged" in text
    assert "not verified: x (http_404)" in text
    assert "matches GitHub" in merge_sync.render("check", _report())


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_check_then_backfill_then_check(db_with_migrations):
    seed = Seed(db_with_migrations)
    job = await seed.job(status="failed", pr_url=URL)
    github = FakeGitHub([pr_object(1, merged=T0 + timedelta(days=1))])
    config = Embry0Config(_env_file=None, database_url=os.environ["TEST_DATABASE_URL"])

    check = await merge_sync.run("check", config=config, client=github)
    assert merge_sync.exit_code("check", check) == 1 and [c["job_id"] for c in check["changes"]] == [job]
    assert await seed.delivery(job) == ("failed", "pr_open")

    backfill = await merge_sync.run("backfill", config=config, client=github)
    assert [c["job_id"] for c in backfill["changes"]] == [job]
    assert await seed.delivery(job) == ("failed", "merged")

    assert (await merge_sync.run("backfill", config=config, client=github))["changes"] == []
    assert merge_sync.exit_code("check", await merge_sync.run("check", config=config, client=github)) == 0


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_check_matches_a_pr_stored_under_a_renamed_repo(db_with_migrations, monkeypatch):
    """The CLI is its own process: it must install REPO_RENAMES itself (#527),
    or a PR stored under the old slug is looked up where it no longer is."""
    from embry0.storage import repo_renames

    monkeypatch.setattr(repo_renames, "REPO_RENAMES", {})
    seed = Seed(db_with_migrations)
    job = await seed.job(status="failed", repo="acme/old", pr_url="https://github.com/acme/old/pull/1")
    github = FakeGitHub([pr_object(1, repo="acme/new", merged=T0 + timedelta(days=1))])
    config = Embry0Config(
        _env_file=None, database_url=os.environ["TEST_DATABASE_URL"], repo_renames='{"acme/old": "acme/new"}'
    )

    check = await merge_sync.run("check", config=config, client=github)

    assert [c["job_id"] for c in check["changes"]] == [job], (
        f"merge-sync did not follow REPO_RENAMES to the PR: {check!r}"
    )


def test_the_command_is_registered(monkeypatch, capsys):
    from embry0.cli import __main__ as cli

    async def fake_run(action):
        return _report(changes=[CHANGE])

    monkeypatch.setattr(merge_sync, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["embry0", "merge-sync", "check"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert "would change: job-1" in capsys.readouterr().out


def test_stdout_carries_only_the_report_so_json_can_be_parsed(monkeypatch, capsys):
    """structlog prints to stdout for the container; for a command whose
    stdout is parsed, its lines have to go to stderr — and the command must
    put the process's configuration back, since structlog's is global."""
    import json as _json

    import structlog

    from embry0.cli import __main__ as cli

    async def fake_run(action):
        structlog.get_logger(__name__).info("merge_sync_recorded", pr_url=URL)
        return _report(changes=[CHANGE])

    monkeypatch.setattr(merge_sync, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["embry0", "merge-sync", "check", "--json"])
    before = structlog.get_config()
    with pytest.raises(SystemExit):
        cli.main()

    captured = capsys.readouterr()
    assert _json.loads(captured.out)["changes"] == [CHANGE]
    assert "merge_sync_recorded" in captured.err
    assert structlog.get_config() == before


def test_render_says_exit_3_is_not_a_failure():
    """Ops reads this text; on this deployment a healthy `check` exits 3
    because four PRs live in repos that are gone or out of reach."""
    text = merge_sync.render("check", _report(unverifiable=[{"pr_url": URL, "reason": "http_404"}]))
    assert "not verified: " in text
    assert "not a failure" in text
    assert "matches GitHub" in text  # the change list is empty, and says so


class _NoDb:
    def __init__(self, *a, **k):
        pass

    async def connect(self):
        pass

    async def close(self):
        pass


def _capture_sweep_client(monkeypatch):
    import embry0.services.merge_sync.sweep as sweep_mod
    import embry0.storage.database as database_mod

    captured = {}

    class _Sweep:
        def __init__(self, *, client, **_):
            captured["client"] = client

        async def run_once(self, **_):
            return {}

    monkeypatch.setattr(database_mod, "DatabasePool", _NoDb)
    monkeypatch.setattr(sweep_mod, "MergeSyncSweep", _Sweep)
    return captured


@pytest.mark.asyncio
async def test_the_sweep_client_resolves_through_the_registry(monkeypatch):
    """Spec §3.1: after the env tokens are removed, an env-only CLI would read GitHub anonymously."""
    from embry0.services.credentials.resolver import CredentialResolver

    captured = _capture_sweep_client(monkeypatch)
    config = Embry0Config(
        database_url="postgresql://x/y", environment_secret_key="placeholder-strong-key", github_token=""
    )
    await merge_sync.run("check", config=config)
    assert isinstance(captured["client"]._credential_resolver, CredentialResolver)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "embry0-dev-secret-key"])
async def test_without_a_real_secret_key_the_sweep_stays_env_only(monkeypatch, key):
    """A dev-mode orchestrator encrypts with an ephemeral key this process cannot know; decrypting
    with the wrong key would mark every registry row decryption_failed, so the CLI must not try."""
    captured = _capture_sweep_client(monkeypatch)
    config = Embry0Config(database_url="postgresql://x/y", environment_secret_key=key, github_token="")
    await merge_sync.run("check", config=config)
    assert captured["client"]._credential_resolver is None
