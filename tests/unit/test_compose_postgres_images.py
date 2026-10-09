"""#324: the Postgres server and its backup client are pinned, together."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _services() -> dict:
    return yaml.safe_load((ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


def test_backup_image_is_pinned_by_digest():
    """A floating `:16` moved pg_dump to 16.10 under a 16.6 server; the older
    psql then rejected the dump's `\\restrict` header."""
    image = _services()["postgres-backup"]["image"]
    assert re.fullmatch(r"prodrigestivill/postgres-backup-local:16@sha256:[0-9a-f]{64}", image), image


def test_server_image_is_an_exact_version():
    image = _services()["postgres"]["image"]
    assert re.fullmatch(r"postgres:\d+\.\d+-alpine", image), image


def test_restore_drill_doc_records_the_pinned_images():
    doc = (ROOT / "docs" / "ops" / "postgres-restore-drill.md").read_text(encoding="utf-8")
    services = _services()
    assert services["postgres"]["image"] in doc
    assert services["postgres-backup"]["image"].split("@", 1)[1] in doc
