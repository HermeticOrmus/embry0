"""The off-host backup service (#581): opt-in, pinned, add-only, and honest
about what it copied.

The script runs here against a stub ``rclone`` that records its arguments and
serves canned listings, so these tests pin the script's own decisions (which
dumps it copies, what it records, when it leaves a status file alone). The
real rclone against a real bucket is the restore drill in
docs/ops/postgres-restore-drill.md.
"""

import gzip
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "infra" / "docker-compose.yml"
SCRIPT = REPO_ROOT / "infra" / "scripts" / "offhost-backup.sh"


def _services() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


# ---- compose wiring ------------------------------------------------------------


def test_the_service_is_opt_in() -> None:
    assert _services()["offhost-backup"].get("profiles") == ["offhost-backup"], (
        "offhost-backup must run only with its profile: without one, every existing deployment "
        "would start copying (or failing to copy) the moment it upgraded."
    )


def test_the_image_is_pinned_by_digest() -> None:
    image = _services()["offhost-backup"]["image"]
    assert re.fullmatch(r"rclone/rclone:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", image), image


def test_the_dumps_are_mounted_read_only() -> None:
    volumes = _services()["offhost-backup"]["volumes"]
    assert "backup-data:/backups:ro" in volumes, volumes


def test_the_orchestrator_reads_the_status_the_service_writes() -> None:
    """The gauge is only as true as this pairing: the service writes /status,
    the orchestrator reads the same volume at the path its config defaults to."""
    from embry0.config import Embry0Config

    services = _services()
    assert "offhost-backup-status:/status" in services["offhost-backup"]["volumes"]
    default_dir = Embry0Config.model_fields["offhost_backup_status_dir"].default
    assert f"offhost-backup-status:{default_dir}:ro" in services["orchestrator"]["volumes"]


def test_one_setting_drives_both_the_copy_and_the_alert() -> None:
    services = _services()
    copy = services["offhost-backup"]["environment"]["OFFHOST_BACKUP_S3_PATH"]
    alert = services["orchestrator"]["environment"]["OFFHOST_BACKUP_S3_PATH"]
    assert copy == alert == "${OFFHOST_BACKUP_S3_PATH:-}"


def test_the_script_never_deletes_off_host() -> None:
    """Add-only is the safety property: a wiped local volume must not be able
    to empty the off-host copy. These rclone verbs remove remote objects."""
    commands = [line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]
    found = sorted({verb for line in commands for verb in re.findall(r"\brclone\s+(\w+)", line)})
    assert found, "found no rclone call in the script: the pattern no longer matches it"
    destructive = {"sync", "move", "moveto", "delete", "deletefile", "purge", "rmdir", "rmdirs", "cleanup", "dedupe"}
    assert not destructive & set(found), f"the script calls rclone {sorted(destructive & set(found))}"
    assert "--delete" not in SCRIPT.read_text()


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _compose_available(), reason="needs the docker compose CLI")
def test_compose_parses_with_the_profile_and_no_offhost_settings(tmp_path: Path) -> None:
    """Turning the profile on must not make compose refuse the whole stack;
    the script reports the missing destination instead."""
    required = sorted(set(re.findall(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*):?\?", COMPOSE.read_text())))
    env_file = tmp_path / "env"
    env_file.write_text("".join(f"{name}=x\n" for name in required))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "OFFHOST_"))}
    env["DOCKER_HOST"] = f"unix://{tmp_path}/no-daemon.sock"
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--env-file", str(env_file), "--profile", "offhost-backup"]
        + ["config", "--services"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"compose refused to parse:\n{result.stderr}"
    assert "offhost-backup" in result.stdout.split()


# ---- the script, against a stub rclone ------------------------------------------

_STUB = """#!/bin/sh
# Records each call; answers lsf from $STUB_DIR/lsf-<n> by the path's last segment.
printf '%s\\n' "$*" >>"$STUB_DIR/calls"
if [ "$1" = copy ] && [ "$2" = --files-from-raw ]; then cp "$3" "$STUB_DIR/copied-list"; fi
if [ "$1" = rcat ]; then cat >/dev/null; fi
if [ "$1" = lsf ]; then
  for last; do :; done
  name=$(basename "$last")
  [ -f "$STUB_DIR/fail-lsf-$name" ] && exit 3
  [ -f "$STUB_DIR/lsf-$name" ] && cat "$STUB_DIR/lsf-$name"
fi
[ -f "$STUB_DIR/fail-$1" ] && exit 1
exit 0
"""


class Run:
    def __init__(self, tmp_path: Path) -> None:
        self.dumps = tmp_path / "last"
        self.status = tmp_path / "status"
        self.stub = tmp_path / "stub"
        bin_dir = tmp_path / "bin"
        for d in (self.dumps, self.status, self.stub, bin_dir):
            d.mkdir()
        rclone = bin_dir / "rclone"
        rclone.write_text(_STUB)
        rclone.chmod(rclone.stat().st_mode | stat.S_IXUSR)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_DIR": str(self.stub),
            "OFFHOST_BACKUP_DUMPS_DIR": str(self.dumps),
            "OFFHOST_BACKUP_STATUS_DIR": str(self.status),
            "OFFHOST_BACKUP_S3_PATH": "/bucket/embry0/",
        }

    def dump(self, name: str, *, complete: bool = True) -> Path:
        path = self.dumps / name
        data = gzip.compress(b"-- PostgreSQL database dump\n" * 100)
        path.write_bytes(data if complete else data[: len(data) // 2])
        return path

    def listing(self, name: str, *times: int) -> None:
        (self.stub / f"lsf-{name}").write_text("".join(f"{t}\n" for t in times))

    def fail(self, what: str) -> None:
        (self.stub / f"fail-{what}").touch()

    def __call__(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(SCRIPT), "once"], env={**self.env, **env}, capture_output=True, text=True, timeout=30
        )

    @property
    def calls(self) -> list[str]:
        path = self.stub / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def copied(self) -> list[str]:
        return (self.stub / "copied-list").read_text().split()

    def recorded(self, dataset: str) -> str | None:
        path = self.status / dataset
        return path.read_text().strip() if path.exists() else None


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


def test_copies_complete_dumps_only(run: Run) -> None:
    run.dump("embry0-20261007-000000.sql.gz")
    run.dump("embry0-20261008-000000.sql.gz", complete=False)  # pg_dump still writing it
    (run.dumps / "embry0-latest.sql.gz").symlink_to("embry0-20261007-000000.sql.gz")
    run.listing("postgres", 1791500000)
    run.listing("minio-last-copy", 1791503600)
    result = run()
    assert result.returncode == 0, result.stderr
    assert run.copied() == ["embry0-20261007-000000.sql.gz"]
    assert "not a complete gzip file yet" in result.stderr


def test_copies_to_the_configured_path_without_its_slashes(run: Run) -> None:
    run.listing("postgres", 1)
    run.listing("minio-last-copy", 1)
    assert run().returncode == 0
    calls = run.calls
    assert any(c.endswith("offhost:bucket/embry0/postgres") and c.startswith("copy --files-from-raw") for c in calls)
    assert "copy embry0minio: offhost:bucket/embry0/minio" in calls
    assert "rcat offhost:bucket/embry0/status/minio-last-copy" in calls


def test_records_the_newest_copy_read_back_from_the_bucket(run: Run) -> None:
    run.listing("postgres", 1791400000, 1791500000, 1791300000)
    run.listing("minio-last-copy", 1791503600)
    assert run().returncode == 0
    assert run.recorded("postgres") == "1791500000"
    assert run.recorded("minio") == "1791503600"


def test_a_failed_read_back_leaves_the_last_status_alone(run: Run) -> None:
    """The gauge must age when the bucket cannot be read, not reset or jump."""
    (run.status / "postgres").write_text("1791000000\n")
    run.fail("lsf-postgres")
    run.listing("minio-last-copy", 1791503600)
    result = run()
    assert result.returncode != 0
    assert run.recorded("postgres") == "1791000000"
    assert "postgres: read-back FAILED" in result.stderr


def test_an_empty_bucket_records_nothing(run: Run) -> None:
    run.listing("postgres")
    run.listing("minio-last-copy")
    result = run()
    assert result.returncode != 0
    assert run.recorded("postgres") is None
    assert run.recorded("minio") is None


def test_a_failed_minio_copy_writes_no_marker(run: Run) -> None:
    """The marker is the MinIO timestamp; writing it after a failed copy
    would report a copy that did not happen."""
    run.fail("copy")
    run.listing("postgres", 1)
    run.listing("minio-last-copy", 1)
    result = run()
    assert result.returncode != 0
    assert not [c for c in run.calls if c.startswith("rcat")]
    assert "minio: copy FAILED" in result.stderr


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"OFFHOST_BACKUP_S3_PATH": ""}, "OFFHOST_BACKUP_S3_PATH is not set"),
        ({"OFFHOST_BACKUP_S3_PATH": "///"}, "OFFHOST_BACKUP_S3_PATH is not set"),
        ({"OFFHOST_BACKUP_INTERVAL_SECONDS": "1h"}, "must be a whole number"),
    ],
)
def test_refuses_a_bad_configuration_before_touching_anything(run: Run, env: dict[str, str], message: str) -> None:
    result = run(**env)
    assert result.returncode == 64
    assert message in result.stderr
    assert run.calls == []


def test_restore_drill_doc_records_the_pinned_image() -> None:
    doc = (REPO_ROOT / "docs" / "ops" / "postgres-restore-drill.md").read_text()
    assert _services()["offhost-backup"]["image"] in doc, "bump the image and re-run the off-host drill together"
