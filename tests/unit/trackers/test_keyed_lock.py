"""KeyedLock — one holder per key, no leak once released (#284)."""

from __future__ import annotations

import asyncio

from embry0.trackers.keyed_lock import KeyedLock


async def test_same_key_is_serialised_and_other_keys_are_not() -> None:
    locks = KeyedLock()
    inside: list[str] = []
    overlap: list[tuple[str, ...]] = []

    async def _work(key: str, tag: str) -> None:
        async with locks.hold(key):
            inside.append(tag)
            overlap.append(tuple(inside))
            await asyncio.sleep(0.01)
            inside.remove(tag)

    await asyncio.gather(_work("ENG-1", "a"), _work("ENG-1", "b"), _work("ENG-2", "c"))

    assert not any({"a", "b"} <= set(seen) for seen in overlap)
    assert any("c" in seen and len(seen) > 1 for seen in overlap)


async def test_released_keys_are_forgotten() -> None:
    locks = KeyedLock()
    async with locks.hold("ENG-1"):
        assert len(locks) == 1
    assert len(locks) == 0


async def test_a_raising_holder_still_releases() -> None:
    locks = KeyedLock()
    try:
        async with locks.hold("ENG-1"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    async with asyncio.timeout(1):
        async with locks.hold("ENG-1"):
            pass
    assert len(locks) == 0
