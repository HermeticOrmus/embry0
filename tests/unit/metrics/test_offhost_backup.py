"""The off-host backup's freshness on /metrics (#581).

The offhost-backup service writes, per dataset, the Unix time of the newest
copy it read back from the off-host bucket. The orchestrator turns those files
into two gauges that the staleness alert reads. The alert can only fire if the
series exist before the first copy ever succeeds and while the files are
missing, so most of these tests pin what a broken or absent status reads as.
"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry

from embry0.api.app import create_app
from embry0.config import Embry0Config
from embry0.metrics.offhost_backup import DATASETS, OffhostBackupCollector

ENABLED = "embry0_offhost_backup_enabled"
LAST = "embry0_offhost_backup_last_success_timestamp_seconds"


@asynccontextmanager
async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield


def _registry(status_dir: Path, *, enabled: bool) -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(OffhostBackupCollector(enabled=enabled, status_dir=status_dir))
    return registry


def _last(registry: CollectorRegistry, dataset: str) -> float | None:
    return registry.get_sample_value(LAST, {"dataset": dataset})


def test_the_datasets_are_the_two_the_service_copies() -> None:
    assert DATASETS == ("postgres", "minio")


def test_reads_each_datasets_timestamp(tmp_path: Path) -> None:
    (tmp_path / "postgres").write_text("1791500000\n")
    (tmp_path / "minio").write_text("1791503600")
    registry = _registry(tmp_path, enabled=True)
    assert registry.get_sample_value(ENABLED) == 1
    assert _last(registry, "postgres") == 1791500000
    assert _last(registry, "minio") == 1791503600


def test_reads_the_files_on_every_scrape(tmp_path: Path) -> None:
    registry = _registry(tmp_path, enabled=True)
    assert _last(registry, "postgres") == 0
    (tmp_path / "postgres").write_text("1791500000")
    assert _last(registry, "postgres") == 1791500000, "a value cached at startup never moves again"


def test_a_missing_status_reads_as_never_copied(tmp_path: Path) -> None:
    """Before the first successful copy, and when the status volume is not
    mounted at all, both series must exist at 0: an absent series keeps a
    staleness alert quiet forever."""
    registry = _registry(tmp_path / "not-mounted", enabled=True)
    for dataset in DATASETS:
        assert _last(registry, dataset) == 0, dataset


@pytest.mark.parametrize("content", ["", "garbage", "-5", "nan", "inf", "1.5e400"])
def test_an_unreadable_status_reads_as_never_copied(tmp_path: Path, content: str) -> None:
    (tmp_path / "postgres").write_text(content)
    value = _last(_registry(tmp_path, enabled=True), "postgres")
    assert value == 0, f"{content!r} read as {value}"


def test_a_directory_in_place_of_the_file_reads_as_never_copied(tmp_path: Path) -> None:
    (tmp_path / "postgres").mkdir()
    assert _last(_registry(tmp_path, enabled=True), "postgres") == 0


def test_disabled_still_exposes_every_series(tmp_path: Path) -> None:
    """Disabled reads enabled=0 and leaves the timestamps at 0, whatever a
    stale status volume still holds: the alert is guarded by the flag."""
    (tmp_path / "postgres").write_text("1791500000")
    registry = _registry(tmp_path, enabled=False)
    assert registry.get_sample_value(ENABLED) == 0
    for dataset in DATASETS:
        assert _last(registry, dataset) == 0, dataset


def test_registering_it_twice_is_refused(tmp_path: Path) -> None:
    """describe() names its metrics, so a duplicate name from a rebase fails
    loudly at registration instead of rendering two series."""
    registry = _registry(tmp_path, enabled=True)
    with pytest.raises(ValueError, match="Duplicated"):
        registry.register(OffhostBackupCollector(enabled=True, status_dir=tmp_path))


def test_config_arms_it_only_when_a_destination_is_set() -> None:
    assert Embry0Config(_env_file=None).offhost_backup_enabled is False
    assert Embry0Config(_env_file=None, offhost_backup_s3_path="  ").offhost_backup_enabled is False
    assert Embry0Config(_env_file=None, offhost_backup_s3_path="bucket/embry0").offhost_backup_enabled is True


@pytest.mark.asyncio
async def test_the_production_app_serves_it(tmp_path: Path) -> None:
    """create_app is what production runs: the collector must be registered
    there, from the config, not only in a test that builds one by hand."""
    (tmp_path / "postgres").write_text("1791500000")
    config = Embry0Config(
        _env_file=None,
        auth_dev_mode=True,
        webhook_dev_mode=True,
        api_key="",
        offhost_backup_s3_path="bucket/embry0",
        offhost_backup_status_dir=str(tmp_path),
    )
    app = create_app(config, lifespan_override=_no_lifespan)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/metrics")).text
    assert f"{ENABLED} 1.0" in body
    assert f'{LAST}{{dataset="postgres"}} 1.7915e+09' in body
    assert f'{LAST}{{dataset="minio"}} 0.0' in body


def test_the_alert_rule_reads_names_the_collector_exposes(tmp_path: Path) -> None:
    """A rename here would leave infra/prometheus/offhost-backup.rules.yml
    matching nothing, and a rule that matches nothing never fires."""
    rules = Path(__file__).resolve().parents[3] / "infra" / "prometheus" / "offhost-backup.rules.yml"
    exprs = [rule["expr"] for group in yaml.safe_load(rules.read_text())["groups"] for rule in group["rules"]]
    referenced = {name for expr in exprs for name in re.findall(r"\bembry0_[a-z0-9_]+", expr)}
    exposed = {family.name for family in _registry(tmp_path, enabled=True).collect()}
    assert referenced == exposed, f"rule reads {sorted(referenced)}, collector exposes {sorted(exposed)}"
