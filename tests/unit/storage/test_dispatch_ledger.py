"""DispatchLedgerRepository — once-per-reason claims and dispatch-failure counts (#284)."""

import asyncio

import pytest

from embry0.storage.repositories.dispatch_ledger import DispatchLedgerRepository

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]


async def test_only_the_first_claim_of_a_reason_wins(db_with_migrations):
    repo = DispatchLedgerRepository(db_with_migrations)

    assert await repo.claim("linear", "ENG-1", "unmapped_repo") is True
    assert await repo.claim("linear", "ENG-1", "unmapped_repo") is False
    # Another reason, another ticket and another tracker are separate notices.
    assert await repo.claim("linear", "ENG-1", "budget") is True
    assert await repo.claim("linear", "ENG-2", "unmapped_repo") is True
    assert await repo.claim("gitlab", "ENG-1", "unmapped_repo") is True


async def test_concurrent_claims_yield_exactly_one_winner(db_with_migrations):
    repo = DispatchLedgerRepository(db_with_migrations)

    results = await asyncio.gather(*(repo.claim("linear", "ENG-1", "cap") for _ in range(8)))

    assert sorted(results) == [False] * 7 + [True]


async def test_release_lets_the_reason_be_claimed_again(db_with_migrations):
    repo = DispatchLedgerRepository(db_with_migrations)
    assert await repo.claim("linear", "ENG-1", "budget") is True

    await repo.release("linear", "ENG-1", "budget")

    assert await repo.claim("linear", "ENG-1", "budget") is True


async def test_failures_are_counted_per_ticket_and_timestamped(db_with_migrations):
    repo = DispatchLedgerRepository(db_with_migrations)

    assert await repo.failure_state("linear", "ENG-1") is None
    assert await repo.record_failure("linear", "ENG-1") == 1
    assert await repo.record_failure("linear", "ENG-1") == 2
    assert await repo.record_failure("linear", "ENG-2") == 1

    state = await repo.failure_state("linear", "ENG-1")
    assert state is not None and state["failures"] == 2 and state["last_failed_at"] is not None


async def test_clear_forgets_one_tickets_notices_and_failures(db_with_migrations):
    repo = DispatchLedgerRepository(db_with_migrations)
    for key in ("ENG-1", "ENG-2"):
        await repo.claim("linear", key, "budget")
        await repo.record_failure("linear", key)

    await repo.clear("linear", "ENG-1")

    assert await repo.claim("linear", "ENG-1", "budget") is True  # announced again next time
    assert await repo.failure_state("linear", "ENG-1") is None
    assert await repo.claim("linear", "ENG-2", "budget") is False
    assert (await repo.failure_state("linear", "ENG-2"))["failures"] == 1
