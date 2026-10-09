"""How fresh the off-host backup copy is, as two gauges on ``/metrics`` (#581).

The ``offhost-backup`` compose service copies the Postgres dumps and the MinIO
buckets to an operator's S3-compatible bucket. After each run it reads the
bucket back and writes, per dataset, one file holding the Unix time of the
newest copy found there:

* ``postgres`` — the modification time of the newest dump in the bucket, so a
  dump job that stopped producing files reads as stale too;
* ``minio`` — the time of the last MinIO copy that finished.

The service's status volume is mounted read-only into the orchestrator, and
this collector reads the files on every scrape. It is a plain collector, not
a ``LoopMetrics`` gauge: the truth is in two small files that another
container owns, so there is nothing to cache and no event to hook.

Both timestamps are always exposed, at 0 when a file is missing or does not
hold a usable number. An absent series would keep a staleness alert quiet
until the first copy succeeded, which is exactly the case it must catch. The
alert is guarded by ``embry0_offhost_backup_enabled`` instead, which comes from
configuration, not from the service being watched.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from pathlib import Path

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

DATASETS = ("postgres", "minio")

_ENABLED = "embry0_offhost_backup_enabled"
_LAST_SUCCESS = "embry0_offhost_backup_last_success_timestamp_seconds"


class OffhostBackupCollector(Collector):
    def __init__(self, *, enabled: bool, status_dir: Path) -> None:
        self._enabled = enabled
        self._status_dir = status_dir

    def describe(self) -> Iterator[GaugeMetricFamily]:
        # Naming the metrics makes a duplicate registration fail loudly.
        yield from self._families(lambda _dataset: 0.0)

    def collect(self) -> Iterator[GaugeMetricFamily]:
        yield from self._families(self._read)

    def _families(self, value_of: Callable[[str], float]) -> Iterator[GaugeMetricFamily]:
        yield GaugeMetricFamily(
            _ENABLED,
            "1 when an off-host backup destination is configured (OFFHOST_BACKUP_S3_PATH). "
            "Guard the staleness alert with it.",
            value=1 if self._enabled else 0,
        )
        last = GaugeMetricFamily(
            _LAST_SUCCESS,
            "Unix time of the newest off-host copy, read back from the bucket; 0 when none is known.",
            labels=["dataset"],
        )
        for dataset in DATASETS:
            last.add_metric([dataset], value_of(dataset))
        yield last

    def _read(self, dataset: str) -> float:
        if not self._enabled:
            return 0.0
        try:
            value = float((self._status_dir / dataset).read_text().strip())
        except (OSError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value >= 0 else 0.0
