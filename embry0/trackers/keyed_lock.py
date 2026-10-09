"""Per-key asyncio lock — serialise work on one ticket, not on all of them (#284).

A label trigger is a read-decide-write sequence (is the ticket tracked? may it
dispatch? create the row, create the job) with awaits between every step. Once
a second entry point exists — the reconciler sweeping while a webhook for the
same ticket is in flight — two coroutines can both read "not tracked" and both
dispatch. One lock per ticket key closes that window inside the orchestrator
process; the ``open -> triaging`` compare-and-swap and the unique tracker-ref
index remain the backstop across processes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class KeyedLock:
    """``async with locks.hold(key)`` — entries are dropped once uncontended."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._holders: dict[str, int] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._holders[key] = self._holders.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._holders[key] -= 1
            if not self._holders[key]:
                # No await between the decrement and the delete, so nobody can
                # be queued on a lock that is about to disappear.
                del self._holders[key]
                del self._locks[key]

    def __len__(self) -> int:
        return len(self._locks)
