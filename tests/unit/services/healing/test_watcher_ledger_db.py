"""The watcher's cap and findings on the REAL ledger (#283, #278 part 2)."""

import pytest
import structlog

from embry0.services.healing.recorder import CaseRecorder
from embry0.services.healing.service import HealingCaseService
from embry0.storage.repositories.healing_cases import HealingCasesRepository
from embry0.storage.repositories.watcher_runs import WatcherRunsRepository

from .conftest import REPO
from .watcher_harness import LINES, build_watcher, proposal

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]


async def _cases(db):
    rows, _ = await HealingCasesRepository(db).list_page(limit=200)
    return {r["title"]: r for r in rows}


async def test_full_cap_defers_then_files_oldest_first_one_per_tick(db_with_migrations, seed):
    db = db_with_migrations
    svc, linear, _ = build_watcher(
        db,
        repo=REPO,
        # #290: each tick a distinct fault, not one window judged twice.
        distinct_windows=True,
        deliverables=[
            proposal("A"),
            proposal("B"),
            proposal("C"),
            proposal("C"),
            proposal("D"),
            "NO_ISSUE",
            "NO_ISSUE",
        ],
    )
    runs, cases_repo = WatcherRunsRepository(db), HealingCasesRepository(db)

    with structlog.testing.capture_logs() as logs:
        assert [(await svc.run_once())["action"] for _ in range(2)] == ["proposed", "proposed"]
        assert await cases_repo.count_open_unaccepted_watcher_cases(days=7, repo=REPO) == 2

        # Cap full: the window is still analysed; C and D are seen, not filed.
        assert (await svc.run_once())["action"] == "deferred"
        assert (await svc.run_once())["action"] == "skipped_duplicate"  # C again
        assert (await svc.run_once())["action"] == "deferred"
        assert [t["identifier"] for t in linear.created] == ["ENG-1", "ENG-2"]
        cases = await _cases(db)
        assert {t: (c["stage"], c["tracker_key"]) for t, c in cases.items()} == {
            "A": ("tracked", "ENG-1"),
            "B": ("tracked", "ENG-2"),
            "C": ("detected", None),
            "D": ("detected", None),
        }
        # 1 at open + the 3 lines of the recurring window. Since #290 a
        # recurrence counts the signature's OCCURRENCES in that window, not
        # the number of ticks that happened to notice it.
        assert cases["C"]["occurrence_count"] == 4
        # Deferred findings hold no slot.
        assert await cases_repo.count_open_unaccepted_watcher_cases(days=7, repo=REPO) == 2

        # A human labels RAV-1: the dispatch path accepts its case, a slot frees.
        issue_id = await seed.issue("[ENG-1] [watcher] A")
        recorder = CaseRecorder(HealingCaseService(cases_repo))
        await recorder.job_dispatched(
            {"id": issue_id, "repo": REPO, "title": "x", "tracker": "linear", "tracker_key": "ENG-1"},
            await seed.job(issue_id),
        )

        # Next tick: the OLDEST deferred finding (C) is filed — and only that one.
        assert (await svc.run_once())["action"] == "no_issue"
        assert [t["identifier"] for t in linear.created] == ["ENG-1", "ENG-2", "ENG-3"]
        assert linear.created[-1]["title"] == "[watcher] C"
        assert "C x40" in linear.created[-1]["description"]  # the proposal its own analysis wrote
        # Cap full again (RAV-2, RAV-3): D keeps waiting.
        assert (await svc.run_once())["action"] == "no_issue"
        assert len(linear.created) == 3

    cases = await _cases(db)
    assert (cases["A"]["stage"], cases["C"]["stage"], cases["C"]["tracker_key"]) == ("accepted", "tracked", "ENG-3")
    assert (cases["D"]["stage"], cases["D"]["tracker_key"]) == ("detected", None)
    # C's run row became the proposed row of RAV-3: one finding, one row, one case.
    run = await runs.get(cases["C"]["watcher_run_id"])
    assert (run["action"], run["tracker_key"]) == ("proposed", "ENG-3")
    assert len(cases) == 4
    # Old and new cap predicates agreed on every one of the seven ticks.
    assert not [e for e in logs if e["event"] == "watcher_cap_predicates_disagree"]
    assert not [e for e in logs if e["log_level"] == "error"]


async def test_each_way_out_frees_the_slot(db_with_migrations, seed):
    """Accepted, merged (embry0 or out-of-band), non-code, cancelled, released."""
    db = db_with_migrations
    titles = ["accepted", "merged", "out_of_band", "non_code", "cancelled", "released", "still-open"]
    # #290: each tick a distinct fault, not one window judged twice.
    svc, linear, _ = build_watcher(
        db, repo=REPO, max_open=99, distinct_windows=True, deliverables=[proposal(t) for t in titles]
    )
    for _ in titles:
        assert (await svc.run_once())["action"] == "proposed"
    cases_repo = HealingCasesRepository(db)
    service = HealingCaseService(cases_repo)
    cases = await _cases(db)
    assert await cases_repo.count_open_unaccepted_watcher_cases(days=7, repo=REPO) == 7

    await service.attach_issue(cases["accepted"]["id"], await seed.issue("[ENG-1] x"))
    await service.record_merge(cases["merged"]["id"], merge_sha="a" * 40, resolution_path="embry0")
    await service.record_merge(cases["out_of_band"]["id"], merge_sha="b" * 40, resolution_path="out_of_band")
    await service.close(cases["non_code"]["id"], resolution_path="non_code")
    linear.states[cases["cancelled"]["tracker_key"]] = "canceled"
    linear.states[cases["released"]["tracker_key"]] = "completed"
    await svc._reconcile_drafts(7)

    assert await cases_repo.open_unaccepted_watcher_tracker_keys(days=7, repo=REPO) == [
        cases["still-open"]["tracker_key"]
    ]
    after = await _cases(db)
    assert (after["cancelled"]["stage"], after["cancelled"]["resolution_path"]) == ("closed", "cancelled")
    # A draft COMPLETED in Linear frees its slot but stays open for out-of-band detection (#282).
    assert after["released"]["stage"] == "tracked"


async def test_the_shadow_compare_notices_a_slot_only_the_ledger_freed(db_with_migrations):
    db = db_with_migrations
    svc, _, _ = build_watcher(db, repo=REPO, deliverables=[proposal("A"), "NO_ISSUE"])
    await svc.run_once()
    (case,) = (await _cases(db)).values()
    # Fixed by a human outside embry0: the old predicate (is there an embry0
    # issue titled [RAV-1]?) cannot see that and still pins the slot.
    await HealingCaseService(HealingCasesRepository(db)).record_merge(
        case["id"], merge_sha="c" * 40, resolution_path="out_of_band"
    )
    with structlog.testing.capture_logs() as logs:
        await svc.run_once()
    (warning,) = [e for e in logs if e["event"] == "watcher_cap_predicates_disagree"]
    assert (warning["ledger_open"], warning["legacy_open"], warning["only_legacy"]) == (0, 1, ["ENG-1"])


async def test_a_full_cap_stops_paying_after_the_days_budget_and_says_so(db_with_migrations):
    """#283 made a full-cap tick analyse; the review of #340 showed that with a
    recurring window it does so every hour forever. The budget bounds it, is
    counted from rows, and survives the restart that would refill a counter."""
    db = db_with_migrations
    runs = WatcherRunsRepository(db)
    quiet = ["NO_ISSUE"] * 8
    svc, linear, jobs = build_watcher(
        db,
        repo=REPO,
        max_open=1,
        deliverables=[proposal("A"), *quiet],
        watcher_max_full_cap_analyses_per_day=3,
    )
    assert (await svc.run_once())["action"] == "proposed"  # cap 1/1 from here

    # Three full-cap ticks analyse (that is the blindness fix) ...
    assert [(await svc.run_once())["action"] for _ in range(3)] == ["no_issue"] * 3
    assert await runs.full_cap_analyses_since(hours=24) == 3

    # ... the fourth is refused, explicitly, and pays nothing.
    spent_jobs = len(jobs.rows)
    out = await svc.run_once()
    assert out["action"] == "skipped_analysis_cap"
    assert out["log_lines"] == len(LINES)  # it still looked at the window
    assert len(jobs.rows) == spent_jobs

    # A restart does not refill the budget: the count is in the rows.
    restarted, _, restarted_jobs = build_watcher(
        db, repo=REPO, max_open=1, deliverables=quiet, linear=linear, watcher_max_full_cap_analyses_per_day=3
    )
    assert (await restarted.run_once())["action"] == "skipped_analysis_cap"
    assert restarted_jobs.rows == {}
    assert len(linear.created) == 1  # nothing was filed by any of it


async def test_a_draft_filed_before_a_lost_write_is_adopted_on_the_next_tick(db_with_migrations, seed):
    """The tracker has no idempotency key: a crash between `create_issue` and
    the ledger write leaves a real ticket nothing points at."""
    db = db_with_migrations
    # #290: each tick a distinct fault, not one window judged twice.
    svc, linear, _ = build_watcher(
        db, repo=REPO, max_open=1, distinct_windows=True, deliverables=[proposal("A"), proposal("B"), "NO_ISSUE"]
    )
    assert (await svc.run_once())["action"] == "proposed"  # RAV-1 for A
    assert (await svc.run_once())["action"] == "deferred"  # B waits, cap 1/1

    # Free the slot: a human labels A, the dispatch path accepts its case.
    cases_repo = HealingCasesRepository(db)
    issue_id = await seed.issue("[ENG-1] [watcher] A")
    await CaseRecorder(HealingCaseService(cases_repo)).job_dispatched(
        {"id": issue_id, "repo": REPO, "title": "A", "tracker": "linear", "tracker_key": "ENG-1"},
        await seed.job(issue_id),
    )
    # The interrupted attempt: the ticket exists, nothing recorded it.
    await linear.create_issue(team_id="team-1", title="[watcher] B", description="...", project_id=None)
    assert [t["identifier"] for t in linear.created] == ["ENG-1", "ENG-2"]

    await svc.run_once()

    # No third ticket: RAV-2 was adopted, and the finding is tracked by it.
    assert [t["identifier"] for t in linear.created] == ["ENG-1", "ENG-2"]
    cases = await _cases(db)
    assert (cases["B"]["stage"], cases["B"]["tracker_key"]) == ("tracked", "ENG-2")
    run = await WatcherRunsRepository(db).get(cases["B"]["watcher_run_id"])
    assert (run["action"], run["tracker_key"]) == ("proposed", "ENG-2")


async def test_a_cancelled_draft_older_than_the_cap_window_still_closes(db_with_migrations):
    """Reconcile used to look only at the proposal window, so a ticket cancelled
    after those 7 days left its case `tracked` forever (#415)."""
    db = db_with_migrations
    svc, linear, _ = build_watcher(db, repo=REPO, max_open=99, deliverables=[proposal("stale")])
    assert (await svc.run_once())["action"] == "proposed"
    case = (await _cases(db))["stale"]
    await db.execute(
        "UPDATE healing_cases SET detected_at = NOW() - interval '30 days' WHERE id = $1",
        case["id"],
    )
    await db.execute(
        "UPDATE watcher_runs SET created_at = NOW() - interval '30 days' WHERE id = $1",
        case["watcher_run_id"],
    )
    linear.states[case["tracker_key"]] = "canceled"

    await svc._reconcile_drafts(7)

    after = await HealingCasesRepository(db).get(case["id"])
    assert after is not None
    assert (after["stage"], after["resolution_path"]) == ("closed", "cancelled"), (
        "a draft cancelled after the proposal window must still close its case; "
        "reconcile was only looking at the cap window (#415)"
    )
