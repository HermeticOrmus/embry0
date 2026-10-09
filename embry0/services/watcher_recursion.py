"""The watcher must not analyse itself (#378).

A stack whose logs include embry0's own — embry0 watching the deployment it
runs in — sees the watcher's tick failures as log lines. Analysing them
would produce an analysis job, whose failure produces more lines, which the
next tick analyses: a loop that also spends money. Measured over the week
before this guard, 43 of the 60 lines the self-stack selector matched were
the watcher's own ``watcher_tick_failed``.

The rule: a structured line whose ``event`` is one of the watcher's own is
dropped before counting, clustering or analysis, whatever the selector says.
"The watcher is broken" is the dead-man alerts' job (``Embry0WatcherNoVerdict3h``
and friends in the monitoring stack), not the watcher's.

Keyed on the event name, not a logger name: structlog's JSON output carries
no logger field, and every event the watcher family emits starts with one of
:data:`OWN_EVENT_PREFIXES` (``tests/unit/services/test_watcher_recursion.py``
pins that against the source). :data:`LOGQL_OWN_EVENTS_EXCLUSION` is the
Loki-side twin, so a selector can spend its line budget on other lines; this
module is the guarantee that holds whatever the selector says.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable

#: Event-name prefixes the watcher family logs under. ``watcher_`` is the
#: tick, its scheduling, filing and health probes; the other two are the
#: signature memory and clustering the tick runs (#289/#290).
OWN_EVENT_PREFIXES: tuple[str, ...] = ("watcher_", "log_signatures_", "log_clustering_")

#: Line filter for a selector. A plain ``!~`` on the raw line, not a ``json``
#: stage, so it cannot touch the plain-text lines that share the stream.
LOGQL_OWN_EVENTS_EXCLUSION = '!~ "\\"event\\": \\"(' + "|".join(OWN_EVENT_PREFIXES) + ')"'


def own_event(line: str) -> str | None:
    """The watcher-family event this structured line reports, or ``None``.

    ``None`` for a plain-text line, a JSON line without an ``event`` field,
    and any event outside :data:`OWN_EVENT_PREFIXES`.
    """
    text = (line or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    event = parsed.get("event") if isinstance(parsed, dict) else None
    if isinstance(event, str) and event.startswith(OWN_EVENT_PREFIXES):
        return event
    return None


def is_own_watcher_line(line: str) -> bool:
    """Did the watcher itself write this line?"""
    return own_event(line) is not None


def drop_own_lines[T](items: Iterable[T], *, line_of: Callable[[T], str] = str) -> tuple[list[T], int]:
    """The items that are not the watcher's own, and how many were.

    Returns the count as well as the survivors so the tick can say what it
    dropped: a window that was *all* watcher lines reads as quiet, and the
    log line is the only record of why.
    """
    kept: list[T] = []
    dropped = 0
    for item in items:
        if is_own_watcher_line(line_of(item)):
            dropped += 1
        else:
            kept.append(item)
    return kept, dropped
