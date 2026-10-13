"""Migration 87 — repos.docker_access, and the backfill that keeps existing DinD users working (#522)."""

import pytest

from embry0.storage.database import DatabasePool
from embry0.storage.migrations.runner import MIGRATIONS


def _sql_for(version: int) -> str:
    match = [(v, d, sql) for v, d, sql in MIGRATIONS if v == version]
    assert len(match) == 1, f"migration {version} must exist exactly once"
    return match[0][2]


def test_migration_87_present_and_gapless() -> None:
    versions = [v for v, _, _ in MIGRATIONS]
    assert 87 in versions, "migration 87 (repos.docker_access, #522) is missing"
    assert versions == list(range(1, len(versions) + 1))


def test_the_column_defaults_to_no_access() -> None:
    sql = _sql_for(87)
    assert "ADD COLUMN IF NOT EXISTS docker_access BOOLEAN NOT NULL DEFAULT FALSE" in sql


# (repo, its stored selection, granted after the backfill?)
_PROFILES = [
    ("qa-jvm", True),
    ("qa-external", False),
    ("qa-jvm-lite", False),  # a prefix of a Docker profile's name is not that profile
    ("team.docker+ci", True),  # regex metacharacters in a name match literally
]

_YAML = {
    "acme/yaml-plain": ("version: 2\ndefaults:\n  sandbox_profile: qa-jvm\n", True),
    "acme/yaml-dquoted": ('version: 2\napp_local:\n  web:\n    sandbox_profile: "qa-jvm"  # boots compose\n', True),
    "acme/yaml-squoted-tab": ("version: 2\napps:\n  web:\n    sandbox_profile:\t'qa-jvm'\n", True),
    "acme/yaml-flow": ("version: 2\napps: {web: {sandbox_profile: team.docker+ci, target: managed}}\n", True),
    "acme/yaml-json-key": ('{"version": 2, "defaults": {"sandbox_profile": "qa-jvm"}}\n', True),
    "acme/yaml-second-layer": (
        "version: 2\ndefaults:\n  sandbox_profile: qa-external\napp_local:\n  api:\n    sandbox_profile: qa-jvm\n",
        True,
    ),
    "acme/yaml-no-docker": ("version: 2\ndefaults:\n  sandbox_profile: qa-external\n", False),
    "acme/yaml-prefix": ("version: 2\ndefaults:\n  sandbox_profile: qa-jvm-lite\n", False),
    "acme/yaml-mode-only": ("version: 2\ndefaults:\n  mode: dind\n", False),  # mode alone never had the daemon
    "acme/yaml-mentions-name": ("version: 2\n# qa-jvm was tried and dropped\ndefaults: {}\n", False),
    "acme/yaml-empty-value": ("version: 2\ndefaults:\n  sandbox_profile:\n  mode: process\n", False),
}

_PREFS = {"acme/pref-docker": ("qa-jvm", True), "acme/pref-slim": ("qa-external", False)}

# (repo, pipeline_template, sandbox_profile, granted?)
_JOBS = [
    ("acme/job-issue", "quick-fix", "qa-jvm", True),
    ("acme/job-untemplated", None, "team.docker+ci", True),
    # Standalone QA stamps qa-jvm on every row as a label: never evidence of use.
    ("acme/job-qa-label", "qa", "qa-jvm", False),
    ("acme/job-no-docker", "quick-fix", "qa-external", False),
]


async def _seed(db: DatabasePool) -> dict[str, bool]:
    expected: dict[str, bool] = {}
    for name, dind in _PROFILES:
        await db.execute(
            "INSERT INTO sandbox_profiles (name, dind_enabled) VALUES ($1, $2)"
            " ON CONFLICT (name) DO UPDATE SET dind_enabled = EXCLUDED.dind_enabled",
            name,
            dind,
        )
    for repo, (yaml_text, granted) in _YAML.items():
        await db.execute("INSERT INTO repos (repo) VALUES ($1)", repo)
        await db.execute("INSERT INTO repo_qa_configs (repo, yaml_text) VALUES ($1, $2)", repo, yaml_text)
        expected[repo] = granted
    for repo, (profile, granted) in _PREFS.items():
        await db.execute("INSERT INTO repos (repo) VALUES ($1)", repo)
        await db.execute("INSERT INTO repo_preferences (repo, sandbox_profile) VALUES ($1, $2)", repo, profile)
        expected[repo] = granted
    for i, (repo, template, profile, granted) in enumerate(_JOBS):
        await db.execute("INSERT INTO repos (repo) VALUES ($1)", repo)
        await db.execute(
            "INSERT INTO jobs (job_id, repo, task, status, pipeline_template, sandbox_profile)"
            " VALUES ($1, $2, 't', 'completed', $3, $4)",
            f"job-{i}",
            repo,
            template,
            profile,
        )
        expected[repo] = granted
    await db.execute("INSERT INTO repos (repo) VALUES ('acme/untouched')")
    expected["acme/untouched"] = False
    return expected


async def _granted(db: DatabasePool) -> dict[str, bool]:
    rows = await db.fetch("SELECT repo, docker_access FROM repos")
    return {r["repo"]: r["docker_access"] for r in rows}


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_backfill_grants_exactly_the_repos_already_on_a_docker_profile(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    expected = await _seed(db)
    assert set((await _granted(db)).values()) == {False}, "new rows must start without access"

    await db.execute(_sql_for(87))

    got = await _granted(db)
    wrong = {repo: got[repo] for repo in expected if got[repo] != expected[repo]}
    assert not wrong, f"backfill decided these repos wrongly (repo -> granted): {wrong}"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_backfill_is_idempotent_and_never_revokes(db_with_migrations: DatabasePool) -> None:
    db = db_with_migrations
    expected = await _seed(db)
    # An admin's earlier grant on a repo the backfill would not pick.
    await db.execute("UPDATE repos SET docker_access = TRUE WHERE repo = 'acme/untouched'")
    expected["acme/untouched"] = True

    for _ in range(2):
        await db.execute(_sql_for(87))
        assert await _granted(db) == expected


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_unregistered_repos_are_never_inserted(db_with_migrations: DatabasePool) -> None:
    """The grant lives on the registry row; the backfill must not register repos as a side effect."""
    db = db_with_migrations
    await db.execute("INSERT INTO sandbox_profiles (name, dind_enabled) VALUES ('qa-jvm', TRUE) ON CONFLICT DO NOTHING")
    await db.execute("INSERT INTO repo_preferences (repo, sandbox_profile) VALUES ('acme/ghost', 'qa-jvm')")
    await db.execute(_sql_for(87))
    assert await db.fetchval("SELECT count(*) FROM repos WHERE repo = 'acme/ghost'") == 0
