"""Reading a healing case back out of its history (#278). Pure.

Every ``healing_case_events`` row stores, under ``detail.updates``, the
exact column change it made to the case — ``opened`` stores the whole
initial row. Folding them in order therefore reproduces the case row, which
is what makes the event log an audit trail rather than a commentary: a row
that disagrees with its own history has been written around the service.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def rebuild_case_row(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold ``events`` (oldest first) into the ``healing_cases`` row they describe.

    Timestamps come back as ISO-8601 strings, as stored in the JSON detail.
    ``created_at`` / ``updated_at`` are bookkeeping, not history, and are
    not reproduced.
    """
    row: dict[str, Any] = {}
    for event in events:
        row.update(event["detail"].get("updates", {}))
    return row
