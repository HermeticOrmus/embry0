"""RunSlots: the gate behind MAX_RUNNING_JOBS (#583)."""

from __future__ import annotations

import asyncio

import pytest

from embry0.orchestration.run_slots import RunSlots


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_zero_means_no_cap():
    slots = RunSlots(0)
    claimed = [slots.claim(f"job-{n}") for n in range(50)]
    assert all(s.granted for s in claimed), "a limit of 0 must grant every claim at once"
    assert slots.running == 50
    assert slots.waiting == 0


@pytest.mark.asyncio
async def test_claims_past_the_limit_wait_in_fifo_order():
    slots = RunSlots(1)
    first = slots.claim("job-1")
    second = slots.claim("job-2")
    third = slots.claim("job-3")
    assert first.granted and not second.granted and not third.granted
    assert (slots.position(second), slots.position(third)) == (1, 2)

    first.release()
    assert second.granted and not third.granted, "the oldest waiter takes the freed slot"
    await asyncio.wait_for(second.wait(), timeout=1)
    assert slots.position(third) == 1


@pytest.mark.asyncio
async def test_priority_claims_go_ahead_of_fresh_ones_in_their_own_order():
    slots = RunSlots(1)
    holder = slots.claim("job-0")
    fresh = slots.claim("fresh")
    resume_a = slots.claim("resume-a", priority=True)
    resume_b = slots.claim("resume-b", priority=True)
    assert [slots.position(s) for s in (resume_a, resume_b, fresh)] == [1, 2, 3]

    holder.release()
    assert resume_a.granted and not resume_b.granted and not fresh.granted
    resume_a.release()
    assert resume_b.granted and not fresh.granted
    resume_b.release()
    assert fresh.granted


@pytest.mark.asyncio
async def test_release_is_idempotent():
    slots = RunSlots(1)
    first = slots.claim("job-1")
    second = slots.claim("job-2")
    third = slots.claim("job-3")
    first.release()
    first.release()
    assert second.granted and not third.granted, "a second release must not free a second slot"
    assert slots.running == 1


@pytest.mark.asyncio
async def test_a_waiter_released_before_its_turn_leaves_the_queue():
    slots = RunSlots(1)
    holder = slots.claim("job-1")
    gone = slots.claim("job-2")
    nxt = slots.claim("job-3")
    gone.release()
    assert slots.waiting == 1 and slots.position(gone) is None
    holder.release()
    assert nxt.granted and not gone.granted


@pytest.mark.asyncio
async def test_a_slot_granted_to_a_cancelled_waiter_is_returned_on_release():
    """The race a cancel can hit: the slot is handed over after the waiting
    task was cancelled but before its ``finally`` ran. The release that
    ``finally`` performs must give the slot back."""
    slots = RunSlots(1)
    holder = slots.claim("job-1")
    waiter = slots.claim("job-2")
    after = slots.claim("job-3")

    task = asyncio.create_task(waiter.wait())
    await _settle()
    task.cancel()
    holder.release()  # grants job-2 before its task has woken up
    with pytest.raises(asyncio.CancelledError):
        await task
    assert waiter.granted and not after.granted

    waiter.release()
    assert after.granted, "the cancelled waiter's slot must pass on to the next job"
    assert slots.running == 1


def test_a_negative_limit_is_refused():
    with pytest.raises(ValueError, match="limit"):
        RunSlots(-1)
