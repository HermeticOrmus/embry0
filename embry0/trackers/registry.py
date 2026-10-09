"""Tracker registry (#160) — mirrors ``embry0/workflows/registry.py``.

Registration replaces: the Linear webhook route rebuilds its service per
delivery (credentials resolve DB-over-env, RAV-1372) and re-registers the
fresh instance, so consumers reading through the registry never see a
stale credential set.
"""

from __future__ import annotations

import structlog

from embry0.trackers.base import IssueTracker

logger = structlog.get_logger(__name__)


class TrackerRegistry:
    def __init__(self) -> None:
        self._trackers: dict[str, IssueTracker] = {}

    def register(self, tracker: IssueTracker) -> None:
        # Same stance as workspace_init's registry: a non-conforming object
        # fails loudly at wire-up time, not at first webhook delivery.
        if not isinstance(tracker, IssueTracker):
            raise TypeError(f"{type(tracker).__name__} does not conform to the IssueTracker protocol")
        self._trackers[tracker.name] = tracker
        logger.info("tracker_registered", name=tracker.name)

    def get(self, name: str) -> IssueTracker | None:
        return self._trackers.get(name)

    def names(self) -> list[str]:
        return sorted(self._trackers)
