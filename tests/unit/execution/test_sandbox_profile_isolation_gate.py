"""#553: SandboxManager.create refuses a profile that would weaken isolation.

The API refuses these values on write. This is the second check, for every
sandbox: a row stored before the API check, or written around it, must fail
closed before any container, enrollment or network exists.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.execution.sandbox_manager import SandboxInitError, SandboxManager
from embry0.safety.error_codes import ErrorCode
from embry0.storage.seeds.sandbox_profiles_builtin import BUILTIN_SANDBOX_PROFILES

# Autouse: every sandbox on sandbox-restricted gets the isolation chain (#554),
# which the docker mock cannot write.
from tests.unit.execution.test_sandbox_manager import _stub_isolation_unless_overridden  # noqa: F401

REGISTRY = "registry:5000"
DIGEST = "sha256:" + "a" * 64

# What SandboxProfilesRepository.upsert stores when a builtin leaves a field out.
_REPO_DEFAULTS = {"cap_drop": ["ALL"], "cap_add": [], "security_opt": ["no-new-privileges"], "extra_networks": []}

# One weakening value per field, each a setup the issue names or one of its kind.
UNSAFE: list[tuple[str, Any]] = [
    ("cap_drop", ["NET_RAW"]),
    ("cap_add", ["ALL"]),
    ("cap_add", ["SYS_ADMIN"]),
    ("security_opt", ["seccomp=unconfined"]),
    ("security_opt", ["apparmor=unconfined"]),
    ("security_opt", ["no-new-privileges", "seccomp=unconfined"]),
    ("security_opt", ["label=disable"]),
    ("base_image", "ghcr.io/example/root-image:latest"),
    ("base_image", "docker.io/library/ubuntu:latest"),
    ("base_image", "localhost:5000/root-image"),
    ("base_image", "--privileged"),
    ("extra_networks", ["bridge"]),
    ("extra_networks", ["host"]),
    ("extra_networks", ["qa-net-job-other"]),
    ("extra_networks", ["sandbox-internet", "bridge"]),
]


class _GrantsEveryRepo:
    """An admin granted Docker access (#522), so qa-jvm's DinD is not what refuses it."""

    async def docker_access(self, repo: str) -> bool:
        return True


def _manager(image_registry: str = "") -> SandboxManager:
    docker = MagicMock()
    docker.run_cmd = AsyncMock(return_value="container-abc123")
    docker.build_run_cmd = MagicMock(return_value=["docker", "run", "..."])
    docker.build_rm_cmd = MagicMock(return_value=["docker", "rm", "..."])
    docker.build_network_cmd = MagicMock(return_value=["docker", "network", "..."])
    proxy_mgr = MagicMock()
    proxy_mgr.enroll_sandbox = AsyncMock(return_value="tok")
    proxy_mgr.unenroll_sandbox = AsyncMock()
    return SandboxManager(
        docker=docker,
        proxy_manager=proxy_mgr,
        image_registry=image_registry,
        docker_access_registry=_GrantsEveryRepo(),
    )


@pytest.fixture(autouse=True)
def _stub_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    from embry0.execution.runtime import egress as egress_mod

    fake = MagicMock()
    fake.container_ip = AsyncMock(return_value="172.20.0.9")
    fake.ensure_helper_image = AsyncMock()
    fake.apply = AsyncMock()
    monkeypatch.setattr(egress_mod, "EgressEnforcer", MagicMock(return_value=fake))


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), UNSAFE)
async def test_create_refuses_a_weakening_profile_before_anything_exists(field: str, value: Any) -> None:
    manager = _manager()
    with pytest.raises(SandboxInitError) as caught:
        await manager.create(job_id="job-weak", profile={"name": "custom", field: value})
    assert caught.value.error_code == ErrorCode.INCOHERENT_CONFIG, caught.value.error_code
    assert "'custom'" in str(caught.value) and f"{field}:" in str(caught.value), str(caught.value)
    manager._docker.build_run_cmd.assert_not_called()
    manager._docker.run_cmd.assert_not_awaited()
    manager._proxy_manager.enroll_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_refuses_the_issues_probe_profile() -> None:
    """The exact profile from #553: every weakening field is named, none reaches docker."""
    manager = _manager(REGISTRY)
    probe = {
        "name": "probe",
        "base_image": "ghcr.io/example/root-image:latest",
        "cap_drop": [],
        "cap_add": ["ALL"],
        "security_opt": ["seccomp=unconfined", "apparmor=unconfined"],
        "extra_networks": ["bridge"],
    }
    with pytest.raises(SandboxInitError) as caught:
        await manager.create(job_id="job-probe", profile=probe)
    for field in ("cap_add:", "security_opt:", "base_image:", "extra_networks:"):
        assert field in str(caught.value), f"{field} missing from: {caught.value}"
    manager._docker.build_run_cmd.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image",
    [
        "embry0-sandbox-qa:latest",
        "registry:5000/embry0-sandbox-qa:latest",
        f"embry0-qa-acme_widgets:2026-10-08T21-12@{DIGEST}",
    ],
)
async def test_create_runs_an_image_from_its_own_registry(image: str) -> None:
    """Pre-built QA images arrive as `<name>:<tag>@<digest>` and some callers pass a qualified name."""
    manager = _manager(REGISTRY)
    await manager.create(job_id="job-own", profile={"name": "qa-external", "base_image": image})
    manager._docker.build_run_cmd.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(BUILTIN_SANDBOX_PROFILES))
async def test_create_runs_every_builtin_profile(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("embry0.execution.sandbox_manager.socket.gethostbyname", lambda host: "172.24.0.3")
    manager = _manager(REGISTRY)
    profile = {"name": name, **_REPO_DEFAULTS, **BUILTIN_SANDBOX_PROFILES[name]}
    await manager.create(job_id=f"job-{name}", profile=profile, repo="acme/widgets")
    kwargs = manager._docker.build_run_cmd.call_args.kwargs
    assert (kwargs["cap_drop"], kwargs["cap_add"], kwargs["security_opt"]) == (["ALL"], [], ["no-new-privileges"])


# --- stored rows ---------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_row_stored_around_the_api_is_refused_at_create(db_with_migrations) -> None:
    """A row from before the API check (or direct SQL) must not reach docker."""
    from embry0.storage.repositories.sandbox_profiles import SandboxProfilesRepository

    repo = SandboxProfilesRepository(db_with_migrations)
    await repo.upsert(name="legacy", cap_add=["ALL"], extra_networks=["bridge"])
    row = await repo.get("legacy")
    assert row is not None

    manager = _manager(REGISTRY)
    with pytest.raises(SandboxInitError, match="cap_add:.*extra_networks:"):
        await manager.create(job_id="job-legacy", profile=row)
    manager._docker.build_run_cmd.assert_not_called()
