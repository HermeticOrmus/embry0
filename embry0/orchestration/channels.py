"""LangGraph channels for JobState keys that carry no value-merging reducer.

A plain TypedDict key compiles to ``LastValue``, which raises
INVALID_CONCURRENT_GRAPH_UPDATE — killing the whole job — when it receives
two writes in one superstep (#304; four jobs died this way after their PRs
existed). A reducer function (``Annotated[T, fn]``) avoids the crash but
LangGraph seeds such a channel with ``T()``, so an unwritten ``str`` key would
read as ``""`` and a ``list`` key as ``[]`` instead of being absent — a silent
behaviour change for every ``state.get(key, default)`` reader.

These channels keep ``LastValue``'s behaviour exactly (absent until written,
same checkpoint shape, a sequential write always replaces) and differ only in
the superstep that used to crash. Unlike a reducer they see all of a
superstep's writes at once, so they can tell a concurrent write from a
sequential one — and they say so: every fold logs
``state_channel_folded_concurrent_writes`` at WARNING. No production graph
fans out today, so that line always means a routing bug dispatched two nodes
in one superstep; the job survives it, the log is how anyone finds out.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import structlog
from langgraph.channels.last_value import LastValue

logger = structlog.get_logger(__name__)

# LangGraph builds these channels from the JobState annotations, so nothing can
# be handed to their constructors. Whoever wants to count folds (#285: the
# orchestrator's metrics) registers here; the default counts nothing.
_fold_observer: Callable[[str, str], None] | None = None


def set_fold_observer(observer: Callable[[str, str], None] | None) -> None:
    """``observer(key, channel_class_name)`` is called on every fold. It must not raise."""
    global _fold_observer
    _fold_observer = observer


class FoldingLastValue(LastValue[Any]):
    """Last-write-wins, also across concurrent writes in one superstep.

    "Last" among concurrent writes is LangGraph's deterministic task-path
    order for the superstep, not wall-clock completion order.
    """

    __slots__ = ()

    def update(self, values: Sequence[Any]) -> bool:
        if len(values) == 0:
            return False
        self.value = values[0] if len(values) == 1 else self._fold(values)
        if len(values) > 1:
            # The kept value's TYPE only — prompts, summaries and urls pass
            # through state and must not be copied into logs.
            logger.warning(
                "state_channel_folded_concurrent_writes",
                key=self.key,
                count=len(values),
                kept_type=type(self.value).__name__,
                channel=type(self).__name__,
            )
            if _fold_observer is not None:
                _fold_observer(str(self.key), type(self).__name__)
        return True

    def _fold(self, values: Sequence[Any]) -> Any:
        """Pick the survivor of two or more same-superstep writes."""
        return values[-1]


class PreferNonEmpty(FoldingLastValue):
    """Among CONCURRENT writes the last non-empty one wins.

    For scalar keys where losing a value is worse than keeping one of two (a
    PR url, the sandbox container id). Precisely:

    - a concurrent clear + write keeps only the write — the ``None`` / ``""``
      cannot erase what a sibling just wrote;
    - two concurrent non-empty writes keep only the LAST; they are not merged
      (these keys hold one value);
    - "last" is LangGraph's task-path order, not wall-clock time;
    - all-empty concurrent writes keep the last of them;
    - a lone write — including a deliberate clear — replaces the value, as it
      always did.

    None of the concurrent cases is reachable in production today: no graph
    fans out. They exist so a future routing bug degrades to a WARNING
    instead of a dead job.
    """

    __slots__ = ()

    def _fold(self, values: Sequence[Any]) -> Any:
        return next((value for value in reversed(values) if value), values[-1])


class UnionOfLists(FoldingLastValue):
    """Concurrent list writes are unioned; a lone write replaces.

    For ``pending_agent_questions``: a question dropped by a fold is never
    asked and nobody notices, which is worse than the crash this module
    replaces. Concurrent non-empty lists are concatenated in task-path order
    and de-duplicated by whole-item equality (question dicts carry no id;
    the same text from two asking nodes is two questions, since the answer
    routes back to the asker). Empty / ``None`` concurrent writes contribute
    nothing; if every concurrent write is empty the last one is kept.

    Single-writer semantics are untouched because the union only ever spans
    ONE superstep's writes: a later node's list replaces the earlier one and
    ``ask_user_interrupt``'s lone ``[]`` still clears. Unreachable in
    production today, like every fold here.
    """

    __slots__ = ()

    def _fold(self, values: Sequence[Any]) -> Any:
        merged: list[Any] = []
        for value in values:
            if isinstance(value, list):
                merged.extend(item for item in value if item not in merged)
        return merged if merged else values[-1]
