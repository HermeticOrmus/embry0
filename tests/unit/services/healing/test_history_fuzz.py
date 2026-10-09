"""Randomised lifecycles: the row always equals its own history (#278).

Each seed drives several cases through a long random sequence of service
calls — legal, illegal, redelivered, out of order, before and after close —
and after every step checks that ``rebuild_case_row(events) == row`` and
that the ledger's invariants hold. Seeded, so a failure reproduces.

The fuzz keeps a little ground truth of its own so it can catch *semantic*
bugs, not only bookkeeping ones: every merge and deploy sha names the fix
attempt it was generated in (``m<attempt>-…`` / ``d<attempt>-…``), and what
an attempt reported is REDELIVERED after a reopen under fresh event keys —
which must never become the retry's merge or anchor. Time only moves
forward (with jitter), as forge timestamps do.
"""

import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from embry0.services.healing.errors import CaseConflictError
from embry0.services.healing.history import rebuild_case_row
from embry0.services.healing.lifecycle import IllegalTransitionError, Stage, fix_deployed
from embry0.services.healing.service import HealingCaseService, TrackerRef, WriteOutcome
from embry0.storage.repositories.healing_cases import HealingCasesRepository

from .conftest import REPO, Seed

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

T = datetime(2026, 8, 1, tzinfo=UTC)
STEPS = 160
POST_CLOSE_STEPS = 6  # keep hitting a closed case this long, then open the next one
PRS = [f"https://github.com/acme/widgets/pull/{n}" for n in (1, 2, 3)]

Call = Callable[[str | None], Awaitable[Any]]


class _Rollback(Exception):
    pass


class Episode:
    """One case plus what the fuzz knows about it that the ledger must agree with."""

    def __init__(self, cid: str, ticket: str) -> None:
        self.cid = cid
        self.ticket = ticket
        self.attempt = 1
        self.closed_for = 0
        self.current: list[Call] = []  # merges/deploys reported in this attempt
        self.stale: list[Call] = []  # ... and in attempts a reopen discarded


def _step(rng: random.Random, service: HealingCaseService, ep: Episode, jobs: list[str], now: datetime) -> Any:
    cid = ep.cid
    at = now - timedelta(minutes=rng.randrange(0, 45))
    key = rng.choice([None, None, f"k-{rng.randrange(12)}"])
    pr = rng.choice(PRS)

    def merge() -> Any:
        args = {
            "merge_sha": rng.choice([None, f"m{ep.attempt}-{rng.randrange(2)}"]),
            "resolution_path": rng.choice(["embry0", "out_of_band"]),
            "pr_url": rng.choice([None, pr]),
            "at": rng.choice([at, at, None]),
        }
        # What gets REDELIVERED later always carries the forge's merge time, as
        # the wiring contract demands: a merge the ledger has never seen,
        # arriving with no time at all, cannot be told from a new one.
        forge = {**args, "at": at}
        ep.current.append(lambda k: service.record_merge(cid, event_key=k, **forge))
        return service.record_merge(cid, event_key=key, **args)

    def deploy() -> Any:
        args = {
            "sha": f"d{ep.attempt}-{rng.randrange(3)}",
            "marker_at": at,
            "services": rng.choice([None, ["backend"], ["ml"]]),
        }
        call: Call = lambda k: service.record_deploy(cid, event_key=k, **args)  # noqa: E731
        ep.current.append(call)
        return call(key)

    async def reopen() -> Any:
        result = await service.reopen(cid, reason="again", at=at, event_key=key)
        if result.outcome is WriteOutcome.APPLIED:
            ep.attempt += 1
            ep.stale += ep.current
            ep.current = []
        return result

    def redeliver_a_discarded_attempt() -> Any:
        if not ep.stale:
            return service.record_recurrence(cid, at=at, event_key=key)
        # Under a fresh delivery id, or none — never the original key.
        return rng.choice(ep.stale)(rng.choice([None, f"redelivered-{rng.randrange(10_000)}"]))

    calls: list[tuple[float, Callable[[], Any]]] = [
        (3, lambda: service.attach_tracker(cid, TrackerRef("linear", ep.ticket), at=at, event_key=key)),
        (4, lambda: service.attach_job(cid, rng.choice(jobs), at=at, event_key=key)),
        (
            6,
            lambda: service.attach_pr(
                cid, url=pr, number=rng.choice([None, 7]), head_sha=rng.choice([None, "a", "b"]),
                reopened=rng.random() < 0.2, at=at, event_key=key,
            ),
        ),
        (3, lambda: service.close_pr(cid, url=pr, at=at, event_key=key)),
        (4, merge),
        (5, deploy),
        (
            2.5,
            lambda: service.record_verdict(
                cid, rng.choice(["healed", "healed", "not_healed", "not_healed", "inconclusive", "regressed"]),
                evidence={"after": rng.randrange(3)}, checked_at=at, event_key=key,
            ),
        ),
        (3, lambda: service.record_recurrence(cid, occurrences=rng.randrange(1, 4), at=at, event_key=key)),
        (4, reopen),
        (4, redeliver_a_discarded_attempt),
        # close is rare, so most of a run exercises the open stages.
        (0.5, lambda: service.close(cid, resolution_path=rng.choice([None, "cancelled", "non_code"]), at=at, event_key=key)),
    ]  # fmt: skip
    return rng.choices([c for _, c in calls], [w for w, _ in calls])[0]()


async def _closable(repo: HealingCasesRepository, cid: str, **kwargs: Any) -> None:
    """close() succeeds — inside a transaction that is then rolled back."""
    try:
        async with repo.atomic() as bound:
            closed = await HealingCaseService(bound).close(cid, **kwargs)
            assert closed["stage"] == Stage.CLOSED
            raise _Rollback
    except _Rollback:
        pass


@pytest.mark.parametrize("rng_seed", [11, 23, 37, 41, 59, 73, 97, 101])
async def test_a_random_lifecycle_never_diverges_from_its_history(
    service: HealingCaseService, cases_repo: HealingCasesRepository, seed: Seed, rng_seed: int
) -> None:
    rng = random.Random(rng_seed)
    jobs = [await seed.job(), await seed.job()]
    refused = episodes = reopens = 0
    stages_seen: set[str] = set()
    effects_seen: set[str] = set()
    ep: Episode | None = None

    for step in range(STEPS):
        now = T + timedelta(hours=step)
        if ep is None or ep.closed_for >= POST_CLOSE_STEPS:
            episodes += 1
            opened = await service.open_case(repo=REPO, origin="watcher", title=f"fuzz {episodes}", at=now)
            ep = Episode(opened["id"], f"ENG-{episodes}")
        try:
            await _step(rng, service, ep, jobs, now)
        except (IllegalTransitionError, CaseConflictError, ValueError):
            refused += 1

        detail = await service.get(ep.cid)
        assert detail is not None
        row = {k: v for k, v in detail.items() if k not in ("jobs", "prs", "events", "created_at", "updated_at")}
        expected = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
        where = f"seed {rng_seed} step {step} case {episodes} attempt {ep.attempt}"
        assert rebuild_case_row(detail["events"]) == expected, where

        stages_seen.add(row["stage"])
        effects_seen.update(e["detail"]["effect"] for e in detail["events"])
        reopens = max(reopens, ep.attempt - 1)
        ep.closed_for += row["stage"] == Stage.CLOSED

        # -- bookkeeping invariants
        if row["deploy_sha"] is not None:
            assert row["merged_at"] is not None and row["deploy_marker_at"] >= row["merged_at"], where
        deployed = fix_deployed(merge_sha=row["merge_sha"], merged_at=row["merged_at"], deployed_at=row["deployed_at"])
        if deployed:
            assert row["deploy_sha"] is not None, where
        if row["stage"] == Stage.PR_OPEN:
            assert any(p["state"] == "open" for p in detail["prs"]), where
        keys = [e["event_key"] for e in detail["events"] if e["event_key"]]
        assert len(keys) == len(set(keys)), where

        # -- semantic invariants
        # The merge and the anchor belong to the CURRENT fix attempt: nothing a
        # reopen discarded may come back as the retry's merge or deploy.
        if row["merge_sha"] is not None:
            assert row["merge_sha"].startswith(f"m{ep.attempt}-"), where
        if row["deploy_sha"] is not None:
            assert row["deploy_sha"].startswith(f"d{ep.attempt}-"), where
        # self_resolved means "no merge"; a case with a merge carries a merge's path.
        if row["resolution_path"] == "self_resolved":
            assert row["merged_at"] is None, where
        if row["merged_at"] is not None and row["stage"] != Stage.CLOSED:
            assert row["resolution_path"] in ("embry0", "out_of_band"), where
        # Every open case can be closed: told a path always; untold, whenever it has one.
        if row["stage"] != Stage.CLOSED:
            await _closable(cases_repo, ep.cid, resolution_path="cancelled")
            if row["resolution_path"] is not None:
                await _closable(cases_repo, ep.cid)

    # The run did real work: most calls landed, across the lifecycle, every
    # way an event can land, and through at least one retry.
    assert refused < STEPS // 2, refused
    assert len(stages_seen) >= 5, stages_seen
    assert effects_seen == {"applied", "repeat", "late", "noted", "post_close"}, effects_seen
    assert reopens >= 1
