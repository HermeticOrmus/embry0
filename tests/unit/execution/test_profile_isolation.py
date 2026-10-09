"""#553: the policy behind both checks on a sandbox profile's isolation fields.

cap_drop/cap_add, security_opt, base_image and extra_networks reach `docker run`
and `docker network connect`. A profile is operator tier, so only the safe
defaults pass. The API check is pinned in tests/unit/api, the create-time check
in test_sandbox_profile_isolation_gate.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from embry0.execution.profile_isolation import field_problem, profile_isolation_problems
from embry0.execution.sandbox_manager import _DEFAULT_PROFILE
from embry0.storage.seeds.sandbox_profiles_builtin import BUILTIN_SANDBOX_PROFILES

REGISTRY = "registry:5000"
DIGEST = "sha256:" + "a" * 64

# What SandboxProfilesRepository.upsert stores when a builtin leaves a field out.
_REPO_DEFAULTS = {"cap_drop": ["ALL"], "cap_add": [], "security_opt": ["no-new-privileges"], "extra_networks": []}

# One weakening value per field, each a setup the issue names or one of its kind.
UNSAFE: list[tuple[str, Any]] = [
    ("cap_drop", ["NET_RAW"]),
    ("cap_drop", ["CHOWN", "SETUID"]),
    ("cap_drop", "ALL"),
    ("cap_add", ["ALL"]),
    ("cap_add", ["SYS_ADMIN"]),
    ("cap_add", ["NET_BIND_SERVICE"]),
    ("security_opt", ["seccomp=unconfined"]),
    ("security_opt", ["apparmor=unconfined"]),
    ("security_opt", ["no-new-privileges", "seccomp=unconfined"]),
    ("security_opt", ["no-new-privileges=false"]),
    ("security_opt", ["label=disable"]),
    ("security_opt", ["systempaths=unconfined"]),
    ("base_image", "ghcr.io/example/root-image:latest"),
    ("base_image", "docker.io/library/ubuntu:latest"),
    ("base_image", "localhost/root-image"),
    ("base_image", "localhost:5000/root-image"),
    ("base_image", "10.0.0.5:5000/root-image"),
    ("base_image", "registry:5000/embry0-sandbox:latest"),  # no registry configured: a foreign host
    ("base_image", "--privileged"),
    ("base_image", "Upper:latest"),
    ("base_image", "embry0-sandbox:latest\n"),
    ("base_image", "embry0-sandbox:latest --privileged"),
    ("base_image", ""),
    ("base_image", None),
    ("extra_networks", ["bridge"]),
    ("extra_networks", ["host"]),
    ("extra_networks", ["qa-net-job-other"]),
    ("extra_networks", ["sandbox-internet", "bridge"]),
    ("extra_networks", "sandbox-internet"),
    ("extra_networks", [{"name": "bridge"}]),
]

SAFE: list[tuple[str, Any]] = [
    ("cap_drop", None),
    ("cap_drop", []),
    ("cap_drop", ["ALL"]),
    ("cap_drop", ["all"]),
    ("cap_drop", ["ALL", "NET_RAW"]),
    ("cap_add", None),
    ("cap_add", []),
    ("security_opt", None),
    ("security_opt", []),
    ("security_opt", ["no-new-privileges"]),
    ("security_opt", ["no-new-privileges=true"]),
    ("security_opt", ["no-new-privileges:true"]),
    ("base_image", "embry0-sandbox:latest"),
    ("base_image", "embry0-sandbox"),
    ("base_image", "team/custom-sandbox:1.2"),
    ("base_image", f"embry0-qa-acme_widgets:2026-10-08T21-12@{DIGEST}"),
    ("extra_networks", None),
    ("extra_networks", []),
    ("extra_networks", ["sandbox-internet"]),
]


@pytest.mark.parametrize(("field", "value"), UNSAFE)
def test_a_weakening_value_is_named(field: str, value: Any) -> None:
    problem = field_problem(field, value)
    assert problem is not None, f"{field}={value!r} weakens isolation and passed the policy"
    assert problem.startswith(f"{field}:"), problem


@pytest.mark.parametrize(("field", "value"), SAFE)
def test_a_safe_value_passes(field: str, value: Any) -> None:
    assert field_problem(field, value) is None, f"{field}={value!r} keeps isolation and was refused"


def test_an_image_in_this_deployments_registry_passes_qualified() -> None:
    assert field_problem("base_image", "registry:5000/embry0-sandbox-qa:latest", REGISTRY) is None
    assert field_problem("base_image", f"registry:5000/embry0-qa-acme_widgets:t@{DIGEST}", REGISTRY) is None
    problem = field_problem("base_image", "other:5000/embry0-sandbox-qa:latest", REGISTRY)
    assert problem is not None, "a registry other than this deployment's must not pass"


def test_an_unknown_field_is_an_error_not_a_pass() -> None:
    with pytest.raises(KeyError):
        field_problem("cap_ad", ["ALL"])


@pytest.mark.parametrize("name", sorted(BUILTIN_SANDBOX_PROFILES))
@pytest.mark.parametrize("registry", ["", REGISTRY])
def test_every_builtin_profile_keeps_isolation(name: str, registry: str) -> None:
    profile = {**_REPO_DEFAULTS, **BUILTIN_SANDBOX_PROFILES[name]}
    assert profile_isolation_problems(profile, registry) == [], f"builtin {name!r} would be refused at create"


def test_the_managers_own_default_profile_keeps_isolation() -> None:
    assert profile_isolation_problems(_DEFAULT_PROFILE) == []


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_builtin_rows_as_stored_keep_isolation(db_with_migrations) -> None:
    from embry0.storage.repositories.sandbox_profiles import SandboxProfilesRepository
    from embry0.storage.seeds.sandbox_profiles_builtin import seed_builtin_sandbox_profiles

    repo = SandboxProfilesRepository(db_with_migrations)
    await seed_builtin_sandbox_profiles(repo)
    rows = await repo.list()
    assert {row["name"] for row in rows} == set(BUILTIN_SANDBOX_PROFILES)
    for row in rows:
        assert profile_isolation_problems(row, REGISTRY) == [], f"stored builtin {row['name']!r} would be refused"
