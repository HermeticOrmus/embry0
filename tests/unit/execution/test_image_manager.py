"""Tests for SandboxImageManager — proxy image presence check + expire paused jobs routing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.execution.image_manager import ContainerReaper, SandboxImageManager
from embry0.execution.runtime import DockerRuntime


@pytest.mark.asyncio
async def test_check_proxy_image_present_returns_true_when_image_exists():
    docker = MagicMock()
    docker._build_base_cmd = MagicMock(return_value=["docker"])
    docker.run_cmd = AsyncMock(return_value=b"some json")
    mgr = SandboxImageManager(DockerRuntime(docker))
    assert await mgr.check_proxy_image_present() is True


@pytest.mark.asyncio
async def test_check_proxy_image_present_returns_false_when_image_missing():
    docker = MagicMock()
    docker._build_base_cmd = MagicMock(return_value=["docker"])
    docker.run_cmd = AsyncMock(side_effect=RuntimeError("No such image"))
    mgr = SandboxImageManager(DockerRuntime(docker))
    assert await mgr.check_proxy_image_present() is False


@pytest.mark.asyncio
async def test_check_proxy_image_present_uses_correct_image_tag():
    docker = MagicMock()
    docker._build_base_cmd = MagicMock(return_value=["docker"])
    docker.run_cmd = AsyncMock(return_value=b"")
    mgr = SandboxImageManager(DockerRuntime(docker))
    await mgr.check_proxy_image_present()
    # The cmd issued should have included embry0-proxy:latest
    called_cmd = docker.run_cmd.call_args[0][0]
    assert "embry0-proxy:latest" in called_cmd
    assert "image" in called_cmd
    assert "inspect" in called_cmd


@pytest.mark.asyncio
async def test_expire_paused_jobs_uses_jobs_repo():
    """_expire_paused_jobs must call jobs_repo.update, not raw db.execute for status."""
    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[{"job_id": "job-abc"}])
    # fetchrow returns None (no associated issue)
    mock_db.fetchrow = AsyncMock(return_value=None)

    mock_jobs_repo = AsyncMock()
    mock_jobs_repo.update = AsyncMock()

    mock_docker = MagicMock()
    mock_docker.build_stop_cmd = MagicMock(return_value=["docker", "stop"])
    mock_docker.build_rm_cmd = MagicMock(return_value=["docker", "rm"])
    mock_docker.run_cmd = AsyncMock(side_effect=RuntimeError("no container"))

    reaper = ContainerReaper(
        DockerRuntime(mock_docker),
        db=mock_db,
        jobs_repo=mock_jobs_repo,
        paused_ttl_hours=0,
    )

    await reaper._expire_paused_jobs()

    # jobs_repo.update must have been called with status="expired"
    mock_jobs_repo.update.assert_awaited_once_with("job-abc", status="expired")

    # Raw db.execute must NOT have been called with a SET status statement
    for c in mock_db.execute.call_args_list:
        assert "SET status" not in str(c), f"raw execute should not set status: {c}"


@pytest.mark.asyncio
async def test_expire_paused_jobs_calls_purge_thread(monkeypatch):
    """After expiring a paused job, purge_thread should be called with database_url + job_id."""
    import embry0.orchestration.checkpoint as ckpt_mod

    purge_calls: list[tuple[str, str]] = []

    async def _fake_purge(database_url: str, thread_id: str) -> None:
        purge_calls.append((database_url, thread_id))

    monkeypatch.setattr(ckpt_mod, "purge_thread", _fake_purge)

    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[{"job_id": "job-purge-test"}])
    mock_db.fetchrow = AsyncMock(return_value=None)

    mock_jobs_repo = AsyncMock()
    mock_jobs_repo.update = AsyncMock()

    mock_docker = MagicMock()
    mock_docker.build_stop_cmd = MagicMock(return_value=["docker", "stop"])
    mock_docker.build_rm_cmd = MagicMock(return_value=["docker", "rm"])
    mock_docker.run_cmd = AsyncMock(side_effect=RuntimeError("no container"))

    reaper = ContainerReaper(
        DockerRuntime(mock_docker),
        db=mock_db,
        jobs_repo=mock_jobs_repo,
        paused_ttl_hours=0,
        database_url="postgresql://test:test@localhost/test",
    )

    await reaper._expire_paused_jobs()

    assert purge_calls == [("postgresql://test:test@localhost/test", "job-purge-test")], (
        f"purge_thread was not called as expected; calls={purge_calls}"
    )


@pytest.mark.asyncio
async def test_expire_paused_jobs_purge_failure_does_not_break_loop(monkeypatch):
    """A purge_thread failure must not propagate — the job is still marked expired."""
    import embry0.orchestration.checkpoint as ckpt_mod

    async def _failing_purge(database_url: str, thread_id: str) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(ckpt_mod, "purge_thread", _failing_purge)

    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[{"job_id": "job-fail-purge"}])
    mock_db.fetchrow = AsyncMock(return_value=None)

    mock_jobs_repo = AsyncMock()
    mock_jobs_repo.update = AsyncMock()

    mock_docker = MagicMock()
    mock_docker.build_stop_cmd = MagicMock(return_value=["docker", "stop"])
    mock_docker.build_rm_cmd = MagicMock(return_value=["docker", "rm"])
    mock_docker.run_cmd = AsyncMock(side_effect=RuntimeError("no container"))

    reaper = ContainerReaper(
        DockerRuntime(mock_docker),
        db=mock_db,
        jobs_repo=mock_jobs_repo,
        paused_ttl_hours=0,
        database_url="postgresql://test:test@localhost/test",
    )

    # Must not raise even though purge_thread raises
    await reaper._expire_paused_jobs()

    # Status update must still have happened
    mock_jobs_repo.update.assert_awaited_once_with("job-fail-purge", status="expired")


@pytest.mark.asyncio
async def test_expire_paused_jobs_no_purge_without_database_url(monkeypatch):
    """When database_url is not set, purge_thread must not be called."""
    import embry0.orchestration.checkpoint as ckpt_mod

    purge_calls: list[tuple[str, str]] = []

    async def _fake_purge(database_url: str, thread_id: str) -> None:
        purge_calls.append((database_url, thread_id))

    monkeypatch.setattr(ckpt_mod, "purge_thread", _fake_purge)

    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[{"job_id": "job-no-url"}])
    mock_db.fetchrow = AsyncMock(return_value=None)

    mock_jobs_repo = AsyncMock()
    mock_jobs_repo.update = AsyncMock()

    mock_docker = MagicMock()
    mock_docker.build_stop_cmd = MagicMock(return_value=["docker", "stop"])
    mock_docker.build_rm_cmd = MagicMock(return_value=["docker", "rm"])
    mock_docker.run_cmd = AsyncMock(side_effect=RuntimeError("no container"))

    reaper = ContainerReaper(
        DockerRuntime(mock_docker),
        db=mock_db,
        jobs_repo=mock_jobs_repo,
        paused_ttl_hours=0,
        # no database_url
    )

    await reaper._expire_paused_jobs()

    assert purge_calls == [], f"purge_thread should not be called without database_url; calls={purge_calls}"


@pytest.mark.asyncio
async def test_expire_paused_jobs_fallback_without_jobs_repo():
    """When jobs_repo is None, the fallback claim is a from-state-guarded
    UPDATE ... RETURNING (RAV-1063) — no unguarded status write remains."""
    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[{"job_id": "job-xyz"}])
    # First fetchrow = the guarded claim (matched); second = issue lookup (none).
    mock_db.fetchrow = AsyncMock(side_effect=[{"job_id": "job-xyz"}, None])

    mock_docker = MagicMock()
    mock_docker.build_stop_cmd = MagicMock(return_value=["docker", "stop"])
    mock_docker.build_rm_cmd = MagicMock(return_value=["docker", "rm"])
    mock_docker.run_cmd = AsyncMock(side_effect=RuntimeError("no container"))

    reaper = ContainerReaper(
        DockerRuntime(mock_docker),
        db=mock_db,
        jobs_repo=None,
        paused_ttl_hours=0,
    )

    await reaper._expire_paused_jobs()

    claim_query = mock_db.fetchrow.call_args_list[0].args[0]
    assert "status = 'expired'" in claim_query
    assert "status = 'paused'" in claim_query, "fallback claim must be from-state guarded"
    assert "RETURNING" in claim_query
    # No raw execute may write status.
    for c in mock_db.execute.call_args_list:
        assert "SET status" not in str(c), f"raw execute should not set status: {c}"


@pytest.mark.asyncio
async def test_expire_paused_jobs_claim_race_skips_destroy_and_purge(monkeypatch):
    """RAV-1063: losing the paused->expired claim (user resumed the job) must
    leave the sandbox, checkpoint, and issue alone. Previously the sandbox
    was destroyed BEFORE the status write, so the race killed live work."""
    import embry0.orchestration.checkpoint as ckpt_mod
    from embry0.storage.repositories.jobs import StatusTransitionConflict

    purge_calls: list[tuple[str, str]] = []

    async def _fake_purge(database_url: str, thread_id: str) -> None:
        purge_calls.append((database_url, thread_id))

    monkeypatch.setattr(ckpt_mod, "purge_thread", _fake_purge)

    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[{"job_id": "job-resumed"}])
    mock_db.fetchrow = AsyncMock(return_value=None)

    mock_jobs_repo = AsyncMock()
    mock_jobs_repo.update = AsyncMock(side_effect=StatusTransitionConflict("resumed"))
    mock_issues_repo = AsyncMock()

    mock_docker = MagicMock()
    mock_docker.build_stop_cmd = MagicMock(return_value=["docker", "stop"])
    mock_docker.build_rm_cmd = MagicMock(return_value=["docker", "rm"])
    mock_docker.run_cmd = AsyncMock()

    reaper = ContainerReaper(
        DockerRuntime(mock_docker),
        db=mock_db,
        jobs_repo=mock_jobs_repo,
        issues_repo=mock_issues_repo,
        paused_ttl_hours=0,
        database_url="postgresql://test:test@localhost/test",
    )

    await reaper._expire_paused_jobs()

    mock_docker.run_cmd.assert_not_called()
    assert purge_calls == []
    mock_issues_repo.cas_transition_on_conn.assert_not_awaited()
    # The issue lookup never happens either — the loop moved on at the claim.
    mock_db.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_expire_paused_jobs_reopens_issue_via_cas_helper():
    """The issue reopen goes through IssuesRepository.cas_transition_on_conn
    with paused as the only expected from-state (RAV-1063) — an issue
    re-dispatched onto a new job must not be knocked back to open."""
    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[{"job_id": "job-abc"}])
    mock_db.fetchrow = AsyncMock(return_value={"issue_id": "iss-9"})

    mock_jobs_repo = AsyncMock()
    mock_issues_repo = AsyncMock()
    mock_issues_repo.cas_transition_on_conn = AsyncMock(return_value=True)

    mock_docker = MagicMock()
    mock_docker.build_stop_cmd = MagicMock(return_value=["docker", "stop"])
    mock_docker.build_rm_cmd = MagicMock(return_value=["docker", "rm"])
    mock_docker.run_cmd = AsyncMock(side_effect=RuntimeError("no container"))

    reaper = ContainerReaper(
        DockerRuntime(mock_docker),
        db=mock_db,
        jobs_repo=mock_jobs_repo,
        issues_repo=mock_issues_repo,
        paused_ttl_hours=0,
    )

    await reaper._expire_paused_jobs()

    mock_issues_repo.cas_transition_on_conn.assert_awaited_once_with(mock_db, "iss-9", "open", {"paused"})


@pytest.mark.asyncio
async def test_ensure_image_defers_to_registry_when_no_build_context(tmp_path):
    """Regression 2026-05-18: in the compose/K8s deploy the orchestrator
    image does NOT ship infra/Dockerfile.sandbox (the sandbox image is
    built+pushed by init-push-images). ensure_image must NOT attempt a
    doomed self-build (`docker build -f /app/infra/Dockerfile.sandbox`,
    which fails with 'lstat /app/infra: no such file or directory'); it
    defers to the registry-provided image and returns False."""
    docker = MagicMock()
    docker._build_base_cmd = MagicMock(return_value=["docker"])
    docker.run_cmd = AsyncMock(return_value=b"ok")  # image inspect succeeds
    mgr = SandboxImageManager(DockerRuntime(docker), app_root=str(tmp_path))  # no infra/ here
    built = await mgr.ensure_image("embry0-sandbox:latest")
    assert built is False
    for call in docker.run_cmd.call_args_list:
        assert "build" not in call[0][0], "must not attempt a self-build"


@pytest.mark.asyncio
async def test_ensure_image_no_context_and_image_missing_returns_false(tmp_path):
    """No build context AND image absent everywhere → return False with a
    clear operator error, still no doomed build attempt."""
    docker = MagicMock()
    docker._build_base_cmd = MagicMock(return_value=["docker"])
    docker.run_cmd = AsyncMock(side_effect=RuntimeError("No such image"))
    mgr = SandboxImageManager(DockerRuntime(docker), app_root=str(tmp_path))
    built = await mgr.ensure_image("embry0-sandbox:latest")
    assert built is False
    for call in docker.run_cmd.call_args_list:
        assert "build" not in call[0][0]


@pytest.mark.asyncio
async def test_ensure_image_self_builds_when_context_present(tmp_path):
    """When infra/Dockerfile.sandbox IS shipped (standalone/dev layout),
    the existing self-build path is preserved unchanged."""
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "Dockerfile.sandbox").write_text("FROM scratch\n")
    docker = MagicMock()
    docker._build_base_cmd = MagicMock(return_value=["docker"])
    docker.run_cmd = AsyncMock(return_value="deadbeef")
    mgr = SandboxImageManager(DockerRuntime(docker), app_root=str(tmp_path))
    built = await mgr.ensure_image("embry0-sandbox:latest", force=True)
    assert built is True
    issued = [c[0][0] for c in docker.run_cmd.call_args_list]
    assert any("build" in cmd for cmd in issued), "self-build must still occur"


class _RegistryRuntime:
    """Runtime stub for #622: the daemon's local store lacks the image; the registry may have it."""

    def __init__(self, *, in_registry: bool) -> None:
        self._in_registry = in_registry
        self.local: set[str] = set()
        self.pulled: list[str] = []
        self.built: list[str] = []

    async def image_exists(self, image: str, *, timeout: int = 60) -> bool:
        return image in self.local

    async def pull_image(self, image: str, *, timeout: int = 180) -> None:
        self.pulled.append(image)
        if not self._in_registry:
            raise RuntimeError("manifest unknown")
        self.local.add(image)

    async def build_image(self, tag: str, **_kwargs) -> None:  # pragma: no cover — must not be called
        self.built.append(tag)


_PINNED = "registry:5000/embry0-sandbox:latest@sha256:" + "5f" * 32


@pytest.mark.asyncio
async def test_ensure_image_pulls_a_pinned_image_the_daemon_has_not_pulled_yet(tmp_path, monkeypatch):
    """#622: after a rebuild the new digest is in the registry but not yet in the daemon's store."""
    from structlog.testing import capture_logs

    import embry0.execution.image_manager as im

    monkeypatch.setattr(im, "qualify_image", lambda image, registry: _PINNED)
    runtime = _RegistryRuntime(in_registry=True)
    mgr = SandboxImageManager(runtime, app_root=str(tmp_path), image_registry="registry:5000")  # type: ignore[arg-type]

    with capture_logs() as logs:
        built = await mgr.ensure_image("embry0-sandbox:latest")

    assert built is False
    errors = [e for e in logs if e["log_level"] == "error"]
    assert not errors, f"an image the registry holds must not be reported missing: {errors}"
    assert runtime.pulled == [_PINNED], f"startup must pull the pinned reference, pulled {runtime.pulled}"
    assert runtime.built == []


@pytest.mark.asyncio
async def test_ensure_image_reports_an_image_absent_from_the_registry_too(tmp_path, monkeypatch):
    from structlog.testing import capture_logs

    import embry0.execution.image_manager as im

    monkeypatch.setattr(im, "qualify_image", lambda image, registry: _PINNED)
    runtime = _RegistryRuntime(in_registry=False)
    mgr = SandboxImageManager(runtime, app_root=str(tmp_path), image_registry="registry:5000")  # type: ignore[arg-type]

    with capture_logs() as logs:
        built = await mgr.ensure_image("embry0-sandbox:latest")

    assert built is False
    errors = [e for e in logs if e["event"] == "sandbox_image_unavailable_no_build_context"]
    assert errors and errors[0]["log_level"] == "error", f"a genuinely absent image is an error, got {logs}"
    assert "manifest unknown" in errors[0]["pull_error"]


@pytest.mark.asyncio
async def test_ensure_image_does_not_pull_an_unqualified_name(tmp_path):
    """Without a registry the name would resolve to Docker Hub; nothing is pulled."""
    runtime = _RegistryRuntime(in_registry=True)
    mgr = SandboxImageManager(runtime, app_root=str(tmp_path))  # type: ignore[arg-type]

    assert await mgr.ensure_image("embry0-sandbox:latest") is False
    assert runtime.pulled == []
