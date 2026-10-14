"""HealingCaseService — the narrow write interface over the ledger (#278)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from embry0.services.healing.errors import CaseConflictError, CaseNotFoundError, CaseReferenceError
from embry0.services.healing.history import rebuild_case_row
from embry0.services.healing.lifecycle import (
    IllegalTransitionError,
    Origin,
    ResolutionPath,
    Stage,
    Verdict,
    fix_deployed,
)
from embry0.services.healing.service import (
    MAX_EVIDENCE_BYTES,
    MAX_SIGNATURE,
    MAX_TEXT,
    HealingCaseService,
    TrackerRef,
    WriteOutcome,
)

from .conftest import REPO, Seed

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

LINEAR = TrackerRef(kind="linear", key="ENG-1988", url="https://linear.app/acme/issue/ENG-1988")
PR = "https://github.com/acme/widgets/pull/7"
PR2 = "https://github.com/acme/widgets/pull/8"
EMBRY0 = ResolutionPath.EMBRY0
T = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


async def _detail(service: HealingCaseService, case_id: str) -> dict:
    detail = await service.get(case_id)
    assert detail is not None
    return detail


async def _events(service: HealingCaseService, case_id: str) -> list[str]:
    return [e["event"] for e in (await _detail(service, case_id))["events"]]


async def _open(service: HealingCaseService, **kwargs: object) -> str:
    kwargs.setdefault("origin", Origin.WATCHER)
    return (await service.open_case(repo=REPO, title="boom", **kwargs))["id"]  # type: ignore[arg-type]


# ---------------------------------------------------------------- open_case


async def test_open_case_starts_detected(service: HealingCaseService) -> None:
    case = await service.open_case(
        repo=REPO,
        origin=Origin.WATCHER,
        title="boom",
        signature='{app="x"} |= "boom"',
        severity="high",
        fixability="code",
    )
    assert case["id"].startswith("hc-")
    assert case["stage"] == Stage.DETECTED
    assert case["origin"] == "watcher"
    assert case["healed_verdict"] == Verdict.PENDING
    assert case["resolution_path"] is None
    assert case["signature_id"] is None
    assert (case["severity"], case["fixability"]) == ("high", "code")
    assert case["occurrence_count"] == 1
    assert case["last_seen_at"] == case["detected_at"]
    assert case["caused_regression_case_id"] is None
    assert await _events(service, case["id"]) == ["opened"]


async def test_open_case_with_a_tracker_starts_tracked(service: HealingCaseService) -> None:
    case = await service.open_case(repo=REPO, origin=Origin.HUMAN, title="boom", tracker=LINEAR)
    assert case["stage"] == Stage.TRACKED
    assert (case["tracker"], case["tracker_key"], case["tracker_url"]) == (LINEAR.kind, LINEAR.key, LINEAR.url)
    assert case["tracked_at"] is not None


@pytest.mark.parametrize("origin", ["alert", "ci"])
async def test_later_detection_sources_are_valid_origins(service: HealingCaseService, origin: str) -> None:
    assert (await service.open_case(repo=REPO, origin=origin, title="boom"))["origin"] == origin


async def test_open_case_is_idempotent_per_watcher_run(service: HealingCaseService, seed: Seed) -> None:
    run_id = await seed.watcher_run()
    first = await _open(service, watcher_run_id=run_id)
    assert await _open(service, watcher_run_id=run_id) == first
    assert await _events(service, first) == ["opened"]


async def test_open_case_is_idempotent_per_open_tracker(service: HealingCaseService) -> None:
    first = await _open(service, origin=Origin.WEBHOOK, tracker=LINEAR)
    assert await _open(service, origin=Origin.WEBHOOK, tracker=LINEAR) == first


async def test_a_closed_case_does_not_swallow_what_follows_it(service: HealingCaseService) -> None:
    # M1: a regression, a reopened ticket or a reused ticket is a NEW case.
    old = await _open(service, tracker=LINEAR)
    await service.close(old, resolution_path=ResolutionPath.NON_CODE)

    new = await service.open_case(repo=REPO, origin=Origin.WATCHER, title="boom again", tracker=LINEAR)
    assert new["id"] != old
    assert new["stage"] == Stage.TRACKED
    # Linked to its predecessor without being told to.
    assert new["parent_case_id"] == old
    # The ticket now resolves to the open case, not the terminal one.
    assert (await service.find_by_tracker(LINEAR.kind, LINEAR.key))["id"] == new["id"]  # type: ignore[index]

    await service.close(new["id"], resolution_path=ResolutionPath.CANCELLED)
    # With nothing open, the newest closed case answers.
    assert (await service.find_by_tracker(LINEAR.kind, LINEAR.key))["id"] == new["id"]  # type: ignore[index]


async def test_an_explicit_parent_wins_over_the_inferred_one(service: HealingCaseService) -> None:
    root = await _open(service)
    old = await _open(service, tracker=LINEAR)
    await service.close(old, resolution_path=ResolutionPath.NON_CODE)
    new = await service.open_case(repo=REPO, origin="watcher", title="x", tracker=LINEAR, parent_case_id=root)
    assert new["parent_case_id"] == root


async def test_open_case_rejects_unknown_references(service: HealingCaseService) -> None:
    with pytest.raises(CaseReferenceError):
        await _open(service, watcher_run_id="wr-missing")
    with pytest.raises(CaseReferenceError):
        await _open(service, parent_case_id="hc-missing")
    with pytest.raises(ValueError):
        await _open(service, origin="robot")


# ------------------------------------------------------------ the happy path


async def test_the_full_embry0_path(service: HealingCaseService, seed: Seed) -> None:
    issue_id = await seed.issue()
    job_id = await seed.job(issue_id)
    marker_at = T + timedelta(minutes=20)

    cid = await _open(service)
    assert (await service.attach_tracker(cid, LINEAR))["stage"] == Stage.TRACKED
    assert (await service.attach_job(cid, job_id))["stage"] == Stage.ACCEPTED
    assert (await service.attach_pr(cid, url=PR, number=7, head_sha="abc123", job_id=job_id, at=T))[
        "stage"
    ] == Stage.PR_OPEN
    merged = await service.record_merge(cid, merge_sha="def456", resolution_path=EMBRY0, pr_url=PR, at=T)
    assert (merged["stage"], merged["merged_at"], merged["resolution_path"]) == (Stage.MERGED, T, EMBRY0)
    deployed = await service.record_deploy(cid, sha="def456", marker_at=marker_at, services=["backend"])
    assert deployed["stage"] == Stage.DEPLOYED
    assert (deployed["deploy_marker_at"], deployed["deploy_services"]) == (marker_at, ["backend"])
    verdict = await service.record_verdict(cid, Verdict.HEALED, evidence={"before": 40, "after": 0})
    assert verdict["stage"] == Stage.VERIFIED
    assert verdict["verdict_evidence"] == {"before": 40, "after": 0}
    closed = await service.close(cid)
    assert (closed["stage"], closed["resolution_path"]) == (Stage.CLOSED, EMBRY0)

    detail = await _detail(service, cid)
    assert detail["issue_id"] == issue_id  # attach_job pulled the issue off the job
    assert [j["job_id"] for j in detail["jobs"]] == [job_id]
    assert [(p["url"], p["number"], p["head_sha"], p["state"], p["merge_sha"], p["job_id"]) for p in detail["prs"]] == [
        (PR, 7, "abc123", "merged", "def456", job_id)
    ]
    assert [e["event"] for e in detail["events"]] == [
        "opened",
        "tracker_attached",
        "accepted",
        "pr_opened",
        "merged",
        "deployed",
        "verdict_recorded",
        "closed",
    ]


async def test_the_case_row_can_be_rebuilt_from_its_events(service: HealingCaseService, seed: Seed) -> None:
    job_id = await seed.job(await seed.issue())
    cid = await _open(service, severity="high", signature="sig", fingerprint="fp")
    await service.attach_job(cid, job_id)
    await service.attach_tracker(cid, LINEAR)  # late
    await service.attach_pr(cid, url=PR, number=7)
    await service.record_recurrence(cid, occurrences=3)
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, pr_url=PR, at=T)
    await service.record_deploy(cid, sha="def", marker_at=T, services=["backend", "ml"])
    await service.record_verdict(cid, Verdict.NOT_HEALED, evidence={"after": 9})
    await service.reopen(cid, reason="still firing")
    await service.record_verdict(cid, Verdict.HEALED, evidence={"after": 0})
    await service.close(cid, resolution_path=ResolutionPath.SELF_RESOLVED, note="done")

    detail = await _detail(service, cid)
    row = {k: v for k, v in detail.items() if k not in ("jobs", "prs", "events", "created_at", "updated_at")}
    expected = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
    assert rebuild_case_row(detail["events"]) == expected
    # And the opened event alone rebuilds the row as it was first written.
    opened = rebuild_case_row(detail["events"][:1])
    assert (opened["id"], opened["stage"], opened["severity"], opened["occurrence_count"]) == (
        cid,
        "detected",
        "high",
        1,
    )


# ------------------------------------------------------------- idempotency


async def test_redelivery_changes_nothing(service: HealingCaseService, seed: Seed) -> None:
    job_id = await seed.job()
    cid = await _open(service)

    async def deliver() -> None:
        await service.attach_tracker(cid, LINEAR)
        await service.attach_job(cid, job_id)
        await service.attach_pr(cid, url=PR, number=7, head_sha="abc", at=T)
        await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, pr_url=PR, at=T)
        await service.record_deploy(cid, sha="def", marker_at=T, services=["backend"])

    await deliver()
    before = await _detail(service, cid)
    await deliver()
    assert await _detail(service, cid) == before


async def test_an_event_key_makes_any_call_exactly_once(service: HealingCaseService) -> None:
    cid = await _open(service)
    for _ in range(3):
        await service.record_recurrence(cid, event_key="loki-tick-41")
    assert (await _detail(service, cid))["occurrence_count"] == 2
    # Without a key every call is a new occurrence.
    await service.record_recurrence(cid)
    await service.record_recurrence(cid)
    assert (await _detail(service, cid))["occurrence_count"] == 4


async def test_every_write_reports_how_it_landed(service: HealingCaseService) -> None:
    cid = await _open(service)
    first = await service.attach_tracker(cid, LINEAR, event_key="d-1")
    assert first.outcome is WriteOutcome.APPLIED
    assert (await service.attach_tracker(cid, LINEAR)).outcome is WriteOutcome.UNCHANGED
    assert (await service.attach_tracker(cid, LINEAR, event_key="d-1")).outcome is WriteOutcome.DUPLICATE


async def test_a_reused_event_key_with_a_different_payload_is_called_out(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.attach_pr(cid, url=PR, number=7, event_key="delivery-9")
    before = await _detail(service, cid)
    clash = await service.attach_pr(cid, url=PR2, number=8, event_key="delivery-9")
    assert clash.outcome is WriteOutcome.DUPLICATE_CONFLICT
    # Dropped like any duplicate — but never silently.
    assert await _detail(service, cid) == before


async def test_concurrent_redeliveries_apply_once(service: HealingCaseService) -> None:
    cid = await _open(service, origin=Origin.WEBHOOK)
    await asyncio.gather(*(service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0) for _ in range(5)))
    assert (await _events(service, cid)).count("merged") == 1


# -------------------------------------------------------------------- links


async def test_a_case_collects_many_jobs(service: HealingCaseService, seed: Seed) -> None:
    issue_id = await seed.issue()
    first, second = await seed.job(issue_id), await seed.job(issue_id)
    cid = await _open(service)
    await service.attach_job(cid, first)
    await service.attach_pr(cid, url=PR)
    # A retry job arrives after the PR: linked, but the stage holds.
    assert (await service.attach_job(cid, second))["stage"] == Stage.PR_OPEN
    assert {j["job_id"] for j in (await _detail(service, cid))["jobs"]} == {first, second}


async def test_attach_issue_accepts_the_case(service: HealingCaseService, seed: Seed) -> None:
    issue_id = await seed.issue()
    case = await service.attach_issue(await _open(service, origin=Origin.HUMAN), issue_id)
    assert (case["stage"], case["issue_id"]) == (Stage.ACCEPTED, issue_id)
    assert case["accepted_at"] is not None


async def test_unknown_references_are_refused(service: HealingCaseService) -> None:
    cid = await _open(service)
    with pytest.raises(CaseReferenceError):
        await service.attach_job(cid, "job-missing")
    with pytest.raises(CaseReferenceError):
        await service.attach_issue(cid, "iss-missing")
    with pytest.raises(CaseReferenceError):
        await service.attach_pr(cid, url=PR, job_id="job-missing")
    detail = await _detail(service, cid)
    assert (detail["stage"], detail["prs"]) == (Stage.DETECTED, [])


async def test_a_case_never_swaps_its_tracker_or_issue(service: HealingCaseService, seed: Seed) -> None:
    cid = await _open(service, tracker=LINEAR)
    with pytest.raises(CaseConflictError):
        await service.attach_tracker(cid, TrackerRef(kind="linear", key="ENG-2"))
    await service.attach_issue(cid, await seed.issue())
    with pytest.raises(CaseConflictError):
        await service.attach_issue(cid, await seed.issue("another"))


async def test_a_tracker_has_one_open_case(service: HealingCaseService) -> None:
    await _open(service, tracker=LINEAR)
    with pytest.raises(CaseConflictError):
        await service.attach_tracker(await _open(service), LINEAR)


# ------------------------------------------------------------ pull requests


async def test_a_superseded_pr_keeps_its_own_outcome(service: HealingCaseService) -> None:
    # M2: PR 1 is closed unmerged, PR 2 replaces it and merges.
    cid = await _open(service)
    await service.attach_pr(cid, url=PR, number=7)
    back = await service.close_pr(cid, url=PR, at=T)
    assert back["stage"] == Stage.ACCEPTED
    await service.attach_pr(cid, url=PR2, number=8)
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, pr_url=PR2)

    detail = await _detail(service, cid)
    assert [(p["number"], p["state"], p["merge_sha"]) for p in detail["prs"]] == [
        (7, "closed", None),
        (8, "merged", "def"),
    ]
    assert detail["prs"][0]["closed_at"] == T
    # A late webhook for EITHER PR still finds its case.
    assert (await service.find_by_pr_url(PR))["id"] == cid  # type: ignore[index]
    assert (await service.find_by_pr_url(PR2))["id"] == cid  # type: ignore[index]


async def test_falling_back_to_accepted_keeps_the_first_acceptance_time(
    service: HealingCaseService, seed: Seed
) -> None:
    cid = await _open(service)
    accepted = (await service.attach_job(cid, await seed.job(), at=T))["accepted_at"]
    await service.attach_pr(cid, url=PR)
    assert (await service.close_pr(cid, url=PR, at=T + timedelta(days=2)))["accepted_at"] == accepted


async def test_a_reopen_waits_for_a_decision_and_starts_the_wait_over(service: HealingCaseService, seed: Seed) -> None:
    """#658: a reopen dispatches nothing, so the case waits at `tracked` for a
    person, aged from the reopen, and the re-acceptance is aged from itself —
    never from the failed attempt's acceptance."""
    cid = await _open(service, tracker=LINEAR, at=T)
    issue_id = await seed.issue()
    await service.attach_job(cid, await seed.job(issue_id), at=T + timedelta(hours=1))
    await service.record_merge(cid, merge_sha="m1", resolution_path=EMBRY0, at=T + timedelta(hours=2))
    await service.record_deploy(cid, sha="d1", marker_at=T + timedelta(hours=3))
    await service.record_verdict(cid, Verdict.NOT_HEALED, evidence={"after": 30})

    reopen_at = T + timedelta(days=2)
    reopened = await service.reopen(cid, reason="still firing", at=reopen_at)
    assert reopened["stage"] == Stage.TRACKED, f"a reopen parked the case at {reopened['stage']!r}, not 'tracked'"
    assert reopened["accepted_at"] is None, "a reopened case kept the failed attempt's accepted_at"
    assert reopened["tracked_at"] == reopen_at, "the wait for the new decision must start at the reopen"
    # The ticket and the issue stay linked: the retry runs on them.
    assert (reopened["tracker_key"], reopened["issue_id"]) == (LINEAR.key, issue_id)

    accepted_again = T + timedelta(days=3)
    retry = await service.attach_job(cid, await seed.job(issue_id), at=accepted_again)
    assert (retry["stage"], retry["accepted_at"]) == (Stage.ACCEPTED, accepted_again)
    assert len((await _detail(service, cid))["jobs"]) == 2


async def test_closing_one_of_two_open_prs_holds_the_stage(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.attach_pr(cid, url=PR)
    await service.attach_pr(cid, url=PR2)
    assert (await service.close_pr(cid, url=PR))["stage"] == Stage.PR_OPEN
    assert (await service.close_pr(cid, url=PR2))["stage"] == Stage.ACCEPTED


async def test_late_news_of_a_closed_pr_does_not_reopen_the_stage(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.attach_pr(cid, url=PR, head_sha="abc", at=T)
    await service.close_pr(cid, url=PR)
    # The `opened` webhook for the same PR is redelivered after the close.
    case = await service.attach_pr(cid, url=PR, head_sha="abc", at=T)
    assert case["stage"] == Stage.ACCEPTED
    assert (await _detail(service, cid))["prs"][0]["state"] == "closed"
    # A genuine reopen on the forge is said explicitly.
    assert (await service.attach_pr(cid, url=PR, reopened=True))["stage"] == Stage.PR_OPEN
    assert (await _detail(service, cid))["prs"][0]["state"] == "open"


async def test_closing_a_merged_pr_is_not_a_close(service: HealingCaseService) -> None:
    # GitHub sends `closed` for a merge too.
    cid = await _open(service)
    await service.attach_pr(cid, url=PR)
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, pr_url=PR)
    before = await _detail(service, cid)
    await service.close_pr(cid, url=PR)
    assert await _detail(service, cid) == before


async def test_an_older_push_cannot_rewind_the_head_sha(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.attach_pr(cid, url=PR, head_sha="one", at=T)
    await service.attach_pr(cid, url=PR, head_sha="two", at=T + timedelta(minutes=5))
    await service.attach_pr(cid, url=PR, head_sha="one", at=T)  # redelivered synchronize
    assert (await _detail(service, cid))["prs"][0]["head_sha"] == "two"
    assert (await _events(service, cid)).count("pr_updated") == 1


async def test_a_head_sha_must_say_when_it_was_pushed(service: HealingCaseService) -> None:
    # "now" would win the ordering guard against every real push time.
    cid = await _open(service)
    with pytest.raises(ValueError, match="at"):
        await service.attach_pr(cid, url=PR, head_sha="one")
    assert (await _detail(service, cid))["prs"] == []


async def test_a_pr_belongs_to_one_case(service: HealingCaseService) -> None:
    await service.attach_pr(await _open(service), url=PR)
    with pytest.raises(CaseConflictError):
        await service.attach_pr(await _open(service), url=PR)


# ------------------------------------------------------------------- merges


async def test_a_redelivered_merge_cannot_flip_the_resolution_path(service: HealingCaseService) -> None:
    # M4: crediting embry0 for a human's fix would corrupt the metrics.
    cid = await _open(service, tracker=LINEAR)
    await service.record_merge(cid, merge_sha="def", resolution_path=ResolutionPath.OUT_OF_BAND)
    case = await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0)
    assert case["resolution_path"] == ResolutionPath.OUT_OF_BAND

    detail = await _detail(service, cid)
    conflict = detail["events"][-1]
    assert conflict["event"] == "merged"
    assert conflict["detail"]["not_applied"] == {"resolution_path": "embry0"}
    # The conflicting redelivery is itself recorded once.
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0)
    assert len((await _detail(service, cid))["events"]) == len(detail["events"])


async def test_record_merge_demands_a_merge_path(service: HealingCaseService) -> None:
    cid = await _open(service)
    with pytest.raises(TypeError):
        await service.record_merge(cid, merge_sha="def")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        await service.record_merge(cid, merge_sha="def", resolution_path=ResolutionPath.CANCELLED)


async def test_a_merge_recorded_without_its_sha_takes_one_later(service: HealingCaseService) -> None:
    cid = await _open(service)
    assert (await service.record_merge(cid, merge_sha=None, resolution_path=EMBRY0, pr_url=PR))["stage"] == Stage.MERGED
    assert (await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, pr_url=PR))["merge_sha"] == "def"
    assert (await _detail(service, cid))["prs"][0]["merge_sha"] == "def"


async def test_an_out_of_band_fix_still_gets_a_verdict(service: HealingCaseService) -> None:
    cid = await _open(service, tracker=LINEAR)
    merged = await service.record_merge(cid, merge_sha="def", resolution_path=ResolutionPath.OUT_OF_BAND, pr_url=PR)
    assert (merged["stage"], merged["resolution_path"]) == (Stage.MERGED, ResolutionPath.OUT_OF_BAND)
    assert (await _detail(service, cid))["prs"][0]["state"] == "merged"
    assert (await service.record_verdict(cid, Verdict.HEALED, evidence={}))["stage"] == Stage.VERIFIED


# --------------------------------------------------------- late events, M5a


async def test_a_late_event_dates_the_stage_it_stands_for(service: HealingCaseService, seed: Seed) -> None:
    cid = await _open(service, origin=Origin.WEBHOOK)
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, at=T)
    opened_at, accepted_at = T - timedelta(hours=2), T - timedelta(hours=3)
    # The PR and job webhooks land after the merge webhook.
    case = await service.attach_pr(cid, url=PR, number=7, at=opened_at)
    case = await service.attach_job(cid, await seed.job(), at=accepted_at)
    assert case["stage"] == Stage.MERGED
    assert (case["pr_open_at"], case["accepted_at"]) == (opened_at, accepted_at)
    # A stage that is already dated is not re-dated.
    case = await service.attach_pr(cid, url=PR2, at=T)
    assert case["pr_open_at"] == opened_at


# ------------------------------------------------------------- deploys, M5b


async def test_the_first_deploy_stays_the_anchor(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, at=T)
    first, later = T, T + timedelta(days=4)
    await service.record_deploy(cid, sha="def", marker_at=first, services=["backend"])
    case = await service.record_deploy(cid, sha="fff", marker_at=later, services=["backend", "ml"])
    assert (case["deploy_sha"], case["deploy_marker_at"], case["deploy_services"]) == ("def", first, ["backend"])
    assert case["deployed_at"] == first

    # The later deploy is history, recorded once.
    await service.record_deploy(cid, sha="fff", marker_at=later, services=["backend", "ml"])
    deploys = [e for e in (await _detail(service, cid))["events"] if e["event"] == "deployed"]
    assert len(deploys) == 2
    assert deploys[1]["detail"]["not_applied"] == {
        "deploy_sha": "fff",
        "deploy_marker_at": later.isoformat(),
        "deploy_services": ["backend", "ml"],
    }


async def _fix_deployed(service: HealingCaseService, cid: str) -> bool:
    row = await _detail(service, cid)
    return fix_deployed(merge_sha=row["merge_sha"], merged_at=row["merged_at"], deployed_at=row["deployed_at"])


async def test_a_deploy_that_beats_its_merge_is_kept_and_anchored_later(service: HealingCaseService) -> None:
    # A fast `./lab deploy` marker reaches embry0 before the merge webhook.
    cid = await _open(service)
    await service.attach_pr(cid, url=PR)
    merged_at, marker = T, T + timedelta(minutes=5)
    case = await service.record_deploy(cid, sha="def", marker_at=marker, services=["backend"])
    assert (case["stage"], case["deploy_sha"], case["deployed_at"]) == (Stage.PR_OPEN, None, None)
    kept = (await _detail(service, cid))["events"][-1]
    assert (kept["event"], kept["detail"]["effect"], kept["to_stage"]) == ("deployed", "noted", "pr_open")

    case = await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, pr_url=PR, at=merged_at)
    assert case["stage"] == Stage.DEPLOYED
    assert (case["deploy_sha"], case["deploy_marker_at"], case["deploy_services"]) == ("def", marker, ["backend"])
    assert case["deployed_at"] == marker
    assert await _fix_deployed(service, cid)
    assert (await _events(service, cid))[-2:] == ["merged", "deployed"]


async def test_the_merge_anchors_on_the_earliest_kept_deploy_not_older_than_it(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.record_deploy(cid, sha="old", marker_at=T - timedelta(hours=3))  # predates the merge
    await service.record_deploy(cid, sha="two", marker_at=T + timedelta(hours=2))
    await service.record_deploy(cid, sha="one", marker_at=T + timedelta(hours=1))
    case = await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, at=T)
    assert (case["deploy_sha"], case["deploy_marker_at"]) == ("one", T + timedelta(hours=1))


async def test_a_deploy_without_a_merge_never_anchors_even_after_a_verdict(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.record_verdict(cid, Verdict.INCONCLUSIVE, evidence={})
    case = await service.record_deploy(cid, sha="aaa", marker_at=T)
    assert (case["stage"], case["deploy_sha"], case["deployed_at"]) == (Stage.VERIFIED, None, None)
    # The merge turns up late and is dated AFTER that deploy: still no fix running.
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, at=T + timedelta(hours=4))
    assert (await _detail(service, cid))["deploy_sha"] is None
    assert not await _fix_deployed(service, cid)


async def test_an_earlier_deploy_delivered_second_becomes_the_anchor(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, at=T)
    await service.record_deploy(cid, sha="two", marker_at=T + timedelta(hours=2))
    case = await service.record_deploy(cid, sha="one", marker_at=T + timedelta(hours=1))
    assert (case["deploy_sha"], case["deploy_marker_at"], case["deployed_at"]) == (
        "one",
        T + timedelta(hours=1),
        T + timedelta(hours=1),
    )


async def test_a_retry_never_anchors_on_a_deploy_vouched_for_the_failed_merge(service: HealingCaseService) -> None:
    # Found by the history fuzz: a same-sha redeploy of attempt 1, dated after
    # the retry's merge time, must not become the retry's anchor.
    cid = await _open(service)
    await service.record_merge(cid, merge_sha="m1", resolution_path=EMBRY0, at=T)
    await service.record_deploy(cid, sha="d1", marker_at=T + timedelta(hours=1))
    await service.record_deploy(cid, sha="d1", marker_at=T + timedelta(hours=9))
    await service.record_verdict(cid, Verdict.NOT_HEALED, evidence={})
    await service.reopen(cid, reason="still firing", at=T + timedelta(hours=1, minutes=30))
    case = await service.record_merge(cid, merge_sha="m2", resolution_path=EMBRY0, at=T + timedelta(hours=2))
    assert (case["stage"], case["deploy_sha"]) == (Stage.MERGED, None)


async def test_a_redelivered_merge_of_the_failed_attempt_is_not_the_retrys_merge(service: HealingCaseService) -> None:
    # Review probe M1. Redelivered under a fresh key, keyless, by sha only, or
    # with a stale time — none of them may turn m1 into attempt 2's merge.
    cid = await _open(service)
    await service.attach_pr(cid, url=PR)
    await service.record_merge(cid, merge_sha="m1", resolution_path=EMBRY0, pr_url=PR, at=T, event_key="gh-1")
    await service.record_deploy(cid, sha="d1", marker_at=T + timedelta(hours=1), event_key="dep-1")
    await service.record_verdict(cid, Verdict.NOT_HEALED, evidence={})
    await service.reopen(cid, reason="still firing", at=T + timedelta(days=1))

    redeliveries = [
        {"merge_sha": "m1", "pr_url": PR, "at": T, "event_key": "gh-1-redelivered"},
        {"merge_sha": "m1", "pr_url": PR, "at": T},
        {"merge_sha": "m1", "pr_url": PR},  # no forge time: the PR is already merged
        {"merge_sha": "m1"},  # no PR either: the sha is a pre-reopen merge's
        {"merge_sha": None, "pr_url": PR2, "at": T + timedelta(hours=2)},  # only the time says it is old
    ]
    for stale in redeliveries:
        case = await service.record_merge(cid, resolution_path=EMBRY0, **stale)  # type: ignore[arg-type]
        assert (case["stage"], case["merge_sha"], case["merged_at"]) == (Stage.TRACKED, None, None), stale
    # ... and so the failed attempt's deploy, redelivered, anchors nothing.
    case = await service.record_deploy(cid, sha="d1", marker_at=T + timedelta(hours=1), event_key="dep-1-again")
    assert case["deploy_sha"] is None
    assert not await _fix_deployed(service, cid)

    noted = [e for e in (await _detail(service, cid))["events"] if e["event"] == "merged"][1:]
    assert noted
    assert all(e["detail"]["effect"] == "noted" and e["to_stage"] == "tracked" for e in noted)
    # The real second merge still lands — here with its sha not yet known ...
    case = await service.record_merge(cid, merge_sha=None, resolution_path=EMBRY0, at=T + timedelta(days=2))
    assert (case["stage"], case["merge_sha"]) == (Stage.MERGED, None)
    # ... and a stale redelivery must not fill that gap with the failed attempt's sha
    # (found by the history fuzz).
    case = await service.record_merge(cid, merge_sha="m1", resolution_path=EMBRY0, at=T + timedelta(days=3))
    assert case["merge_sha"] is None
    assert (await service.record_merge(cid, merge_sha="m2", resolution_path=EMBRY0))["merge_sha"] == "m2"


async def test_replacing_the_anchor_takes_the_whole_deploy_and_voids_the_verdict(service: HealingCaseService) -> None:
    # Review probes m1 + m2.
    cid = await _open(service)
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, at=T)
    await service.record_deploy(cid, sha="dB", marker_at=T + timedelta(hours=5), services=["ml"])
    await service.record_verdict(cid, Verdict.HEALED, evidence={"after": 0})

    case = await service.record_deploy(cid, sha="dA", marker_at=T + timedelta(hours=1))  # services unknown
    assert (case["deploy_sha"], case["deploy_services"]) == ("dA", None)
    # A verdict measured from dB says nothing about dA: confirm-healed re-measures.
    assert (case["healed_verdict"], case["verdict_evidence"], case["verdict_checked_at"]) == ("pending", None, None)
    replaced = (await _detail(service, cid))["events"][-1]["detail"]
    assert replaced["deploy"]["deploy_sha"] == "dA"
    assert replaced["anchor_replaced_after_verdict"]["verdict"] == "healed"
    assert replaced["anchor_replaced_after_verdict"]["evidence"] == {"after": 0}


async def test_a_retry_is_never_judged_from_the_failed_attempts_deploy(service: HealingCaseService) -> None:
    # The reviewer's probe: merge m1 → deploy A → not_healed → reopen → merge m2.
    cid = await _open(service)
    day = timedelta(days=1)
    await service.record_merge(cid, merge_sha="m1", resolution_path=EMBRY0, at=T)
    await service.record_deploy(cid, sha="dA", marker_at=T + timedelta(hours=3))
    assert await _fix_deployed(service, cid)
    await service.record_verdict(cid, Verdict.NOT_HEALED, evidence={"after": 30})

    reopened = await service.reopen(cid, reason="still firing", at=T + timedelta(hours=12))
    assert reopened["stage"] == Stage.TRACKED
    for column in ("merge_sha", "merged_at", "deploy_sha", "deploy_marker_at", "deploy_services", "deployed_at"):
        assert reopened[column] is None, column
    assert not await _fix_deployed(service, cid)

    merged = await service.record_merge(cid, merge_sha="m2", resolution_path=EMBRY0, at=T + day)
    assert (merged["stage"], merged["merge_sha"], merged["deploy_sha"]) == (Stage.MERGED, "m2", None)
    assert not await _fix_deployed(service, cid)

    deployed = await service.record_deploy(cid, sha="dC", marker_at=T + day + timedelta(hours=1))
    assert (deployed["deploy_sha"], deployed["stage"]) == ("dC", Stage.DEPLOYED)
    assert await _fix_deployed(service, cid)
    # The failed attempt is still in the history.
    first = next(e for e in (await _detail(service, cid))["events"] if e["event"] == "deployed")
    assert first["detail"]["updates"]["deploy_sha"] == "dA"


# ----------------------------------------------------------------- verdicts


async def test_a_finding_can_heal_with_no_fix_at_all(service: HealingCaseService) -> None:
    # OQ2: RAV-1529 stopped by itself; RAV-1582 was a vendor top-up.
    cid = await _open(service, tracker=LINEAR)
    verdict = await service.record_verdict(cid, Verdict.HEALED, evidence={"after": 0})
    assert verdict["stage"] == Stage.VERIFIED
    closed = await service.close(cid, resolution_path=ResolutionPath.SELF_RESOLVED)
    assert closed["resolution_path"] == ResolutionPath.SELF_RESOLVED
    # Verified and closed — yet nothing was ever merged or deployed.
    assert (closed["merge_sha"], closed["deployed_at"]) == (None, None)


async def test_healing_without_a_merge_is_self_resolved_at_once(service: HealingCaseService) -> None:
    # #285 must not have to wait for close() to tell these from real fixes.
    cid = await _open(service)
    case = await service.record_verdict(cid, Verdict.HEALED, evidence={"after": 0})
    assert case["resolution_path"] == ResolutionPath.SELF_RESOLVED
    # Revised: it was not healed after all, so nothing resolved it yet.
    case = await service.record_verdict(cid, Verdict.NOT_HEALED, evidence={"after": 7})
    assert case["resolution_path"] is None
    assert (await service.close(cid, resolution_path=ResolutionPath.NON_CODE))["resolution_path"] == "non_code"


@pytest.mark.parametrize("verdict", [Verdict.NOT_HEALED, Verdict.INCONCLUSIVE, Verdict.REGRESSED])
async def test_only_a_healed_verdict_resolves_a_case_by_itself(service: HealingCaseService, verdict: Verdict) -> None:
    assert (await service.record_verdict(await _open(service), verdict, evidence={}))["resolution_path"] is None


async def test_healing_is_credited_to_the_fix_when_there_is_one(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.record_merge(cid, merge_sha="def", resolution_path=ResolutionPath.OUT_OF_BAND, at=T)
    await service.record_deploy(cid, sha="def", marker_at=T)
    assert (await service.record_verdict(cid, Verdict.HEALED, evidence={}))["resolution_path"] == "out_of_band"


async def test_healing_after_a_failed_fix_is_not_credited_to_that_fix(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, at=T)
    await service.record_deploy(cid, sha="def", marker_at=T)
    await service.record_verdict(cid, Verdict.NOT_HEALED, evidence={})
    await service.reopen(cid, reason="still firing")
    # No second fix: it stopped by itself.
    case = await service.record_verdict(cid, Verdict.HEALED, evidence={})
    assert case["resolution_path"] == ResolutionPath.SELF_RESOLVED
    assert not await _fix_deployed(service, cid)


async def test_a_late_merge_replaces_self_resolved(service: HealingCaseService) -> None:
    # Review probe M2: a case with a merge keeps the MERGE's path, whenever the merge turns up.
    cid = await _open(service)
    await service.record_verdict(cid, Verdict.HEALED, evidence={})
    case = await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, at=T)
    assert (case["stage"], case["resolution_path"], case["merge_sha"]) == (Stage.VERIFIED, "embry0", "def")
    # Revising the verdict no longer strips the path of a case that has a merge ...
    case = await service.record_verdict(cid, Verdict.NOT_HEALED, evidence={})
    assert case["resolution_path"] == "embry0"
    # ... so the case can always be closed without being told a path.
    assert (await service.close(cid))["resolution_path"] == "embry0"


async def test_pending_is_not_a_recordable_verdict(service: HealingCaseService) -> None:
    with pytest.raises(ValueError):
        await service.record_verdict(await _open(service), Verdict.PENDING, evidence={})


async def test_a_verdict_can_be_revised(service: HealingCaseService) -> None:
    cid = await _open(service)
    first = await service.record_verdict(cid, Verdict.HEALED, evidence={"after": 0})
    revised = await service.record_verdict(cid, Verdict.REGRESSED, evidence={"after": 12})
    assert (revised["healed_verdict"], revised["verdict_evidence"]) == (Verdict.REGRESSED, {"after": 12})
    assert revised["verified_at"] == first["verified_at"]
    assert (await _events(service, cid)).count("verdict_recorded") == 2


async def test_not_healed_earns_a_reopen(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.record_merge(cid, merge_sha="def", resolution_path=EMBRY0, at=T)
    await service.record_deploy(cid, sha="def", marker_at=T)
    await service.record_verdict(cid, Verdict.NOT_HEALED, evidence={"after": 38})
    reopened = await service.reopen(cid, reason="still firing", at=T + timedelta(hours=6))
    assert (reopened["stage"], reopened["healed_verdict"], reopened["resolution_path"]) == (
        Stage.TRACKED,
        Verdict.PENDING,
        None,
    )
    detail = await _detail(service, cid)
    verdict_event = next(e for e in detail["events"] if e["event"] == "verdict_recorded")
    assert verdict_event["detail"]["updates"]["healed_verdict"] == "not_healed"
    # The retry advances again with its own merge and its own deploy anchor.
    retry = T + timedelta(days=1)
    assert (await service.record_merge(cid, merge_sha="fed", resolution_path=EMBRY0, at=retry))["merge_sha"] == "fed"
    assert (await service.record_deploy(cid, sha="fed", marker_at=retry))["deploy_marker_at"] == retry


async def test_a_healed_case_cannot_be_reopened(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.record_verdict(cid, Verdict.HEALED, evidence={})
    with pytest.raises(CaseConflictError):
        await service.reopen(cid, reason="why not")


# --------------------------------------------------------------- recurrence


async def test_recurrence_counts_and_never_moves_the_case(service: HealingCaseService) -> None:
    cid = await _open(service, at=T)
    seen = T + timedelta(hours=6)
    case = await service.record_recurrence(cid, occurrences=4, at=seen)
    assert (case["stage"], case["occurrence_count"], case["last_seen_at"]) == (Stage.DETECTED, 5, seen)
    # An older sighting still counts but does not move last_seen_at back.
    case = await service.record_recurrence(cid, at=T + timedelta(hours=1))
    assert (case["occurrence_count"], case["last_seen_at"]) == (6, seen)


# ------------------------------------------------------------------ abandon


async def test_abandoning_an_accepted_case_closes_it(service: HealingCaseService, seed: Seed) -> None:
    # #410: accepted, attempted, given up. Before this the case had no
    # terminal state at all and sat at `accepted` forever.
    cid = await _open(service)
    issue_id = await seed.issue()
    await service.attach_job(cid, await seed.job(issue_id))
    assert (await service.get(cid))["stage"] == Stage.ACCEPTED

    closed = await service.abandon(cid, note="already fixed on main by e4099d5")
    assert (closed["stage"], closed["resolution_path"]) == (Stage.CLOSED, ResolutionPath.ABANDONED)
    assert closed["note"] == "already fixed on main by e4099d5"
    assert closed["closed_at"] is not None


async def test_abandoning_never_closes_a_case_whose_fix_is_out_there(service: HealingCaseService, seed: Seed) -> None:
    # A PR outlives the job that opened it, so one job giving up must not
    # close the case — the change request is still there to merge or close.
    cid = await _open(service)
    await service.attach_job(cid, await seed.job())
    await service.attach_pr(cid, url=PR, number=7)
    assert (await service.get(cid))["stage"] == Stage.PR_OPEN

    held = await service.abandon(cid, note="gave up on the pipeline")
    assert held["stage"] == Stage.PR_OPEN, "an abandoned job must not close a case with an open PR"
    assert held["resolution_path"] is None, "and it must not claim the case was abandoned"
    assert held["note"] is None
    # ... while the attempt is still in the history, held rather than applied.
    events = [e for e in (await _detail(service, cid))["events"] if e["event"] == "abandoned"]
    assert len(events) == 1, events
    assert events[0]["detail"]["not_applied"] == {
        "resolution_path": ResolutionPath.ABANDONED,
        "note": "gave up on the pipeline",
    }


async def test_abandoning_a_reopened_retry_closes_it(service: HealingCaseService, seed: Seed) -> None:
    # A `not_healed` verdict sends a case back to `tracked` for a person to
    # accept another attempt (#658). Until someone does there is no attempt
    # to give up; once the retry's job is linked, giving up on THAT attempt is
    # an abandon like any other.
    cid = await _open(service)
    issue_id = await seed.issue()
    await service.attach_job(cid, await seed.job(issue_id))
    await service.record_verdict(cid, Verdict.NOT_HEALED, evidence={"occurrences": 12})
    await service.reopen(cid, reason="it came back")
    assert (await service.get(cid))["stage"] == Stage.TRACKED
    assert (await service.abandon(cid, note="the old job"))["stage"] == Stage.TRACKED

    await service.attach_job(cid, await seed.job(issue_id))
    closed = await service.abandon(cid, note="nothing left to try")
    assert (closed["stage"], closed["resolution_path"]) == (Stage.CLOSED, ResolutionPath.ABANDONED)


async def test_abandoning_twice_writes_once(service: HealingCaseService, seed: Seed) -> None:
    cid = await _open(service)
    await service.attach_job(cid, await seed.job())
    first = await service.abandon(cid, event_key="job-abandoned:job-1")
    again = await service.abandon(cid, event_key="job-abandoned:job-1")
    assert first["stage"] == again["stage"] == Stage.CLOSED
    assert again.outcome is WriteOutcome.DUPLICATE


# -------------------------------------------------------------------- close


async def test_closing_an_unresolved_case_needs_a_path(service: HealingCaseService) -> None:
    cid = await _open(service)
    with pytest.raises(ValueError):
        await service.close(cid)
    closed = await service.close(cid, resolution_path=ResolutionPath.NON_CODE, note="vendor credits ran out")
    assert (closed["stage"], closed["resolution_path"], closed["note"]) == (
        Stage.CLOSED,
        ResolutionPath.NON_CODE,
        "vendor credits ran out",
    )
    assert closed["closed_at"] is not None


async def test_what_happens_after_close_is_history_not_an_error(service: HealingCaseService, seed: Seed) -> None:
    # M6: a cancelled ticket is fixed out of band weeks later (#282).
    cid = await _open(service, tracker=LINEAR)
    closed = await service.close(cid, resolution_path=ResolutionPath.CANCELLED)

    await service.attach_pr(cid, url=PR, number=7)
    case = await service.record_merge(cid, merge_sha="def", resolution_path=ResolutionPath.OUT_OF_BAND, pr_url=PR)
    await service.attach_job(cid, await seed.job())
    await service.record_recurrence(cid)

    # The case row is frozen ...
    assert case == closed
    detail = await _detail(service, cid)
    assert (detail["stage"], detail["resolution_path"], detail["merge_sha"]) == ("closed", "cancelled", None)
    # ... while the facts and the history are kept.
    assert [(p["url"], p["state"], p["merge_sha"]) for p in detail["prs"]] == [(PR, "merged", "def")]
    assert len(detail["jobs"]) == 1
    late = detail["events"][-4:]
    assert [e["event"] for e in late] == ["pr_opened", "merged", "accepted", "recurrence"]
    assert all(e["detail"]["effect"] == "post_close" and e["to_stage"] == "closed" for e in late)
    assert late[1]["detail"]["not_applied"] == {"merge_sha": "def", "resolution_path": "out_of_band"}

    # Redelivered, none of it is recorded twice.
    await service.record_merge(cid, merge_sha="def", resolution_path=ResolutionPath.OUT_OF_BAND, pr_url=PR)
    assert len((await _detail(service, cid))["events"]) == len(detail["events"])


async def test_a_closed_case_is_never_reopened(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.close(cid, resolution_path=ResolutionPath.CANCELLED)
    # Closing again is a redelivery, even with a different path.
    assert (await service.close(cid, resolution_path=ResolutionPath.NON_CODE))["resolution_path"] == "cancelled"
    with pytest.raises(IllegalTransitionError):
        await service.reopen(cid, reason="it is back")


# -------------------------------------------------------------------- reads


async def test_unknown_case(service: HealingCaseService) -> None:
    assert await service.get("hc-missing") is None
    with pytest.raises(CaseNotFoundError):
        await service.attach_pr("hc-missing", url=PR)


async def test_finders(service: HealingCaseService, seed: Seed) -> None:
    issue_id = await seed.issue()
    cid = await _open(service, tracker=LINEAR)
    await service.attach_issue(cid, issue_id)
    assert (await service.find_by_tracker("linear", "ENG-1988"))["id"] == cid  # type: ignore[index]
    assert (await service.find_by_issue(issue_id))["id"] == cid  # type: ignore[index]
    assert await service.find_by_tracker("linear", "ENG-0") is None
    assert await service.find_by_pr_url(PR) is None


async def test_find_by_job(service: HealingCaseService, seed: Seed) -> None:
    """SH-P4.4: the evidence resolver asks "which case is this job working?"."""
    job_id = await seed.job(await seed.issue())
    other = await seed.job()
    cid = await _open(service)
    await service.attach_job(cid, job_id)
    assert (await service.find_by_job(job_id))["id"] == cid  # type: ignore[index]
    assert await service.find_by_job(other) is None
    assert await service.find_by_job("job-missing") is None


# ------------------------------------------------- bounded history, text caps


async def test_a_closed_signature_that_keeps_firing_costs_one_history_row(service: HealingCaseService) -> None:
    cid = await _open(service)
    await service.close(cid, resolution_path=ResolutionPath.CANCELLED)
    rows = len((await _detail(service, cid))["events"])
    for hour in range(25):
        await service.record_recurrence(cid, occurrences=2, at=T + timedelta(hours=hour), event_key=f"tick-{hour}")
    await service.record_recurrence(cid, at=T, event_key="tick-24")  # redelivered

    detail = await _detail(service, cid)
    assert len(detail["events"]) == rows + 1
    noted = detail["events"][-1]["detail"]
    assert (noted["effect"], noted["occurrences"]) == ("post_close", 50)
    assert (noted["first_seen_at"], noted["last_seen_at"]) == (T.isoformat(), (T + timedelta(hours=24)).isoformat())
    # The case row stays frozen.
    assert detail["occurrence_count"] == 1


async def test_an_over_long_signature_is_refused_never_truncated(service: HealingCaseService) -> None:
    # The signature is EXECUTED (confirm-healed runs it as LogQL). A truncated
    # query is a different, possibly broader, query.
    with pytest.raises(ValueError, match="signature"):
        await service.open_case(repo=REPO, origin="watcher", title="t", signature="x" * (MAX_SIGNATURE + 1))
    ok = await service.open_case(repo=REPO, origin="watcher", title="t", signature="x" * MAX_SIGNATURE)
    assert len(ok["signature"]) == MAX_SIGNATURE


@pytest.mark.parametrize("bad", [datetime(2026, 9, 1, 12, 0), datetime(2099, 1, 1, tzinfo=UTC)])  # noqa: DTZ001
async def test_timestamps_must_be_aware_and_not_in_the_future(service: HealingCaseService, bad: datetime) -> None:
    # A merge dated 2099 would leave every real deploy NOTED forever.
    cid = await _open(service)
    calls = [
        lambda: service.open_case(repo=REPO, origin="watcher", title="t", at=bad),
        lambda: service.attach_tracker(cid, LINEAR, at=bad),
        lambda: service.attach_pr(cid, url=PR, head_sha="a", at=bad),
        lambda: service.close_pr(cid, url=PR, at=bad),
        lambda: service.record_merge(cid, merge_sha="m", resolution_path=EMBRY0, at=bad),
        lambda: service.record_deploy(cid, sha="d", marker_at=bad),
        lambda: service.record_verdict(cid, Verdict.HEALED, evidence={}, checked_at=bad),
        lambda: service.record_recurrence(cid, at=bad),
        lambda: service.close(cid, resolution_path="cancelled", at=bad),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="timezone-aware|future"):
            await call()
    assert await _events(service, cid) == ["opened"]
    # A little clock skew between hosts is tolerated.
    await service.attach_tracker(cid, LINEAR, at=datetime.now(UTC) + timedelta(minutes=2))


async def test_free_text_is_capped_at_the_service_boundary(service: HealingCaseService) -> None:
    long = "x" * 20_000
    case = await service.open_case(repo=REPO, origin="watcher", title=long, note=long)
    for column in ("title", "note"):
        assert len(case[column]) == MAX_TEXT[column]
        assert case[column].endswith("…[truncated]")
    await service.record_verdict(case["id"], Verdict.NOT_HEALED, evidence={})
    await service.reopen(case["id"], reason=long)
    reason = (await _detail(service, case["id"]))["events"][-1]["detail"]["reason"]
    assert len(reason) == MAX_TEXT["reason"]


async def test_evidence_is_counts_and_queries_not_log_lines(service: HealingCaseService) -> None:
    cid = await _open(service)
    with pytest.raises(ValueError, match="evidence"):
        await service.record_verdict(cid, Verdict.HEALED, evidence={"lines": ["boom " * 50] * 400})
    assert MAX_EVIDENCE_BYTES == 16_384
    assert (await _detail(service, cid))["stage"] == Stage.DETECTED
