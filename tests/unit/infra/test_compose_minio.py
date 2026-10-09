"""#573: the MinIO service pulls from a live source by exact version and
digest, carries no credentials in its healthcheck, and does not hold the
orchestrator back."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def _services() -> dict:
    return yaml.safe_load((ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


def test_minio_image_is_an_exact_release_pinned_by_digest():
    """quay.io/minio/minio and docker.io/minio/minio stopped serving every tag."""
    image = _services()["minio"]["image"]
    assert re.fullmatch(r"docker\.io/pgsty/silo:RELEASE\.\d{4}-\d\d-\d\dT\d\d-\d\d-\d\dZ@sha256:[0-9a-f]{64}", image), (
        image
    )


def test_minio_keeps_the_data_volume_and_command():
    """The existing minio-data volume is mounted where MinIO wrote it."""
    minio = _services()["minio"]
    assert "minio-data:/data" in minio["volumes"]
    assert minio["command"].split()[:2] == ["server", "/data"]
    assert "entrypoint" not in minio and "user" not in minio, "the image's root entrypoint owns the data"


def test_minio_healthcheck_carries_no_credentials():
    """#605: `mc alias set` read a root password starting with `-` as a flag."""
    test = _services()["minio"]["healthcheck"]["test"]
    joined = " ".join(test)
    assert "MINIO_ROOT" not in joined, test
    assert "/minio/health/ready" in joined, test


def test_orchestrator_does_not_wait_for_minio():
    """It prepares the bucket in the background and retries (#573)."""
    depends = _services()["orchestrator"].get("depends_on", {})
    assert "minio" not in depends, depends


def test_third_party_notices_name_the_minio_image():
    image = _services()["minio"]["image"].split(":", 1)[0]
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert f"`{image}`" in notices, image
