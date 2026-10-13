"""CaseRecorder — the pipeline's fail-open reports to the ledger (#278 part 2)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from embry0.services.healing.recorder import DEFERRED_NOTE, CaseRecorder
from embry0.services.healing.service import TrackerRef

from .conftest import REPO

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]


OPENED = datetime(2026, 9, 20, 8, 26, tzinfo=UTC)


class _PrTimes:
    """GitHubPrTimes, stubbed: what the forge says each PR was opened at."""

    def __init__(self, answers: dict[str, datetime] | None = None) -> None:
        self.answers = answers if answers is not None else {}
        self.asked: list[str] = []

    async def opened_at(self, url: str) -> datetime | None:
        self.asked.append(url)
        return self.answers.get(url)


@pytest.fixture
def pr_times() -> _PrTimes:
    return _PrTimes()


@pytest.fixture
def recorder(service, pr_times) -> CaseRecorder:
    return CaseRecorder(service, pr_times=pr_times)


def _issue(issue_id: str, key: str | None = "ENG-1", tracker: str | None = "linear") -> dict:
    return {
        "id": issue_id,
        "repo": REPO,
        "title": f"[{key}] Fix the thing",
        "tracker": tracker,
        "tracker_key": key,
        "tracker_url": f"https://linear.app/x/{key}" if key else None,
    }


# ---- watcher ---------------------------------------------------------------------


async def test_a_filed_finding_is_one_tracked_watcher_case_with_its_links(recorder, service, seed):
    run_id = await seed.watcher_run("ENG-1")
    case_id = await recorder.finding_recorded(
        repo=REPO,
        title="Fix the thing",
        watcher_run_id=run_id,
        fingerprint="fp-1",
        severity="high",
        tracker=TrackerRef("linear", "ENG-1", "https://linear.app/x/ENG-1"),
    )
    case = await service.get(case_id)
    assert (case["origin"], case["stage"], case["watcher_run_id"]) == ("watcher", "tracked", run_id)
    assert (case["tracker"], case["tracker_key"], case["tracker_url"]) == (
        "linear",
        "ENG-1",
        "https://linear.app/x/ENG-1",
    )
    assert (case["fingerprint"], case["severity"], case["note"]) == ("fp-1", "high", None)

    # Reported twice (a retried tick, the startup backfill): still one case.
    again = await recorder.finding_recorded(
        repo=REPO, title="x", watcher_run_id=run_id, fingerprint="fp-1", severity=None, tracker=None
    )
    assert again == case_id


async def test_a_deferred_finding_waits_at_detected_and_holds_no_slot(recorder, service, cases_repo, seed):
    run_id = await seed.watcher_run(None)
    case_id = await recorder.finding_recorded(
        repo=REPO, title="Later", watcher_run_id=run_id, fingerprint="fp-2", severity="low", tracker=None
    )
    case = await service.get(case_id)
    assert (case["stage"], case["tracker_key"], case["note"]) == ("detected", None, DEFERRED_NOTE)
    assert await cases_repo.count_open_unaccepted_watcher_cases(days=7) == 0
    assert [f["id"] for f in await cases_repo.deferred_watcher_findings(repo=REPO)] == [case_id]

    # Filed when a slot frees: now it is a draft and DOES hold a slot.
    assert await recorder.deferred_finding_filed(case_id, TrackerRef("linear", "ENG-9"), watcher_run_id=run_id)
    assert (await service.get(case_id))["stage"] == "tracked"
    assert await cases_repo.count_open_unaccepted_watcher_cases(days=7) == 1
    assert await cases_repo.deferred_watcher_findings(repo=REPO) == []
    # ... exactly once.
    events = len((await service.get(case_id))["events"])
    await recorder.deferred_finding_filed(case_id, TrackerRef("linear", "ENG-9"), watcher_run_id=run_id)
    assert len((await service.get(case_id))["events"]) == events


async def test_a_dropped_deferred_finding_is_closed_as_cancelled(recorder, service, seed):
    case_id = await recorder.finding_recorded(
        repo=REPO, title="t", watcher_run_id=await seed.watcher_run(None), fingerprint=None, severity=None, tracker=None
    )
    assert await recorder.deferred_finding_dropped(case_id, "why")
    case = await service.get(case_id)
    assert (case["stage"], case["resolution_path"], case["note"]) == ("closed", "cancelled", "why")


async def test_a_recurrence_counts_once_per_tick_on_the_findings_case(recorder, service, seed):
    case_id = await recorder.finding_recorded(
        repo=REPO,
        title="t",
        watcher_run_id=await seed.watcher_run("ENG-1"),
        fingerprint="fp-r",
        severity=None,
        tracker=TrackerRef("linear", "ENG-1"),
    )
    for run in ("wr-tick-1", "wr-tick-1", "wr-tick-2"):  # the first tick is redelivered
        await recorder.finding_recurred(fingerprint="fp-r", repo=REPO, watcher_run_id=run)
    case = await service.get(case_id)
    assert case["occurrence_count"] == 3
    assert case["stage"] == "tracked"

    with structlog.testing.capture_logs() as logs:
        await recorder.finding_recurred(fingerprint="fp-unknown", repo=REPO, watcher_run_id="wr-tick-3")
    assert [e["event"] for e in logs] == ["healing_recurrence_without_case"]


async def test_a_released_draft_closes_only_an_unaccepted_case(recorder, service, seed):
    draft = await recorder.finding_recorded(
        repo=REPO,
        title="a",
        watcher_run_id=await seed.watcher_run("ENG-1"),
        fingerprint=None,
        severity=None,
        tracker=TrackerRef("linear", "ENG-1"),
    )
    accepted = await recorder.finding_recorded(
        repo=REPO,
        title="b",
        watcher_run_id=await seed.watcher_run("ENG-2", title="b"),
        fingerprint=None,
        severity=None,
        tracker=TrackerRef("linear", "ENG-2"),
    )
    await service.attach_issue(accepted, await seed.issue())

    await recorder.draft_released(TrackerRef("linear", "ENG-1"), state="cancelled")
    await recorder.draft_released(TrackerRef("linear", "ENG-2"), state="cancelled")
    await recorder.draft_released(TrackerRef("linear", "ENG-404"), state="cancelled")  # no case: nothing to do

    closed = await service.get(draft)
    assert (closed["stage"], closed["resolution_path"]) == ("closed", "cancelled")
    assert closed["note"] == "The draft ticket was cancelled in the tracker before anyone accepted it."
    assert (await service.get(accepted))["stage"] == "accepted"


# ---- dispatch ----------------------------------------------------------------------


async def test_dispatching_a_labelled_watcher_draft_accepts_its_case(recorder, service, cases_repo, seed):
    draft = await recorder.finding_recorded(
        repo=REPO,
        title="t",
        watcher_run_id=await seed.watcher_run("ENG-1"),
        fingerprint=None,
        severity=None,
        tracker=TrackerRef("linear", "ENG-1"),
    )
    assert await cases_repo.count_open_unaccepted_watcher_cases(days=7) == 1
    issue_id = await seed.issue()
    job_id = await seed.job(issue_id)

    assert await recorder.job_dispatched(_issue(issue_id), job_id) == draft

    case = await service.get(draft)
    assert (case["origin"], case["stage"], case["issue_id"]) == ("watcher", "accepted", issue_id)
    assert [j["job_id"] for j in case["jobs"]] == [job_id]
    assert await cases_repo.count_open_unaccepted_watcher_cases(days=7) == 0  # acceptance frees the slot


async def test_dispatching_a_ticket_a_human_wrote_opens_a_human_case(recorder, service, cases_repo, seed):
    issue_id = await seed.issue("[ENG-50] Human wrote this")
    job_id = await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), job_id)

    case = await service.get(case_id)
    assert (case["origin"], case["stage"], case["tracker_key"], case["issue_id"]) == (
        "human",
        "accepted",
        "ENG-50",
        issue_id,
    )
    assert case["repo"] == REPO
    assert await cases_repo.count_open_unaccepted_watcher_cases(days=7) == 0  # never a watcher slot


async def test_an_abandoned_job_closes_its_case_as_abandoned(recorder, service, seed):
    # #410: the pipeline's report for "a person gave up and nothing shipped".
    issue_id = await seed.issue("[ENG-50] t")
    job_id = await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), job_id)

    assert await recorder.job_abandoned(issue_id=issue_id, job_id=job_id, note="Abandoned after 0 retries") == case_id

    case = await service.get(case_id)
    assert (case["stage"], case["resolution_path"]) == ("closed", "abandoned")
    assert case["note"] == "Abandoned after 0 retries"


async def test_an_abandoned_job_with_no_case_is_not_an_error(recorder, seed):
    # Fail-open like every other report: a job whose issue has no case (a
    # dispatch from before the ledger) must not raise into the job lifecycle.
    assert await recorder.job_abandoned(issue_id=await seed.issue("[ENG-99] t"), job_id="job-x") is None
    assert await recorder.job_abandoned(issue_id=None, job_id="job-x") is None


async def test_a_retry_adds_a_job_to_the_same_case_and_is_idempotent(recorder, service, seed):
    issue_id = await seed.issue("[ENG-50] t")
    first, retry = await seed.job(issue_id), await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), first)
    assert await recorder.job_dispatched(_issue(issue_id, "ENG-50"), retry) == case_id
    assert await recorder.job_dispatched(_issue(issue_id, "ENG-50"), retry) == case_id  # reported twice

    case = await service.get(case_id)
    assert [j["job_id"] for j in case["jobs"]] == [first, retry]
    assert [e["event_key"] for e in case["events"] if e["event_key"]] == [f"job:{first}", f"job:{retry}"]


async def test_a_ticket_whose_case_is_closed_gets_a_new_linked_case(recorder, service, seed):
    issue_id = await seed.issue("[ENG-50] t")
    old = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), await seed.job(issue_id))
    await service.close(old, resolution_path="cancelled")

    new = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), await seed.job(issue_id))
    assert new != old
    assert (await service.get(new))["parent_case_id"] == old


async def test_an_issue_with_no_case_and_no_case_opening_tracker_records_nothing(recorder, cases_repo, seed):
    issue_id = await seed.issue("plain")
    job_id = await seed.job(issue_id)
    assert await recorder.job_dispatched(_issue(issue_id, None, None), job_id) is None
    # A tracker embry0 does not file into opens nothing.
    assert await recorder.job_dispatched(_issue(issue_id, "PROJ-1", "jira"), job_id) is None
    assert (await cases_repo.list_page())[1] == 0


async def test_a_dispatched_github_issue_opens_a_case_in_its_own_repo(recorder, cases_repo, seed):
    """GitHub joined the case-opening trackers once the ledger's ticket key
    became repo-qualified (#385) — issue #42 exists in every repo that has 42
    issues, so before that a case for it could have been any repo's."""
    issue_id = await seed.issue("gh")
    job_id = await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "42", "github"), job_id)
    assert case_id is not None
    case = await cases_repo.get(case_id)
    assert (case["tracker"], case["tracker_key"], case["repo"]) == ("github", "42", REPO)


# ---- PRs ---------------------------------------------------------------------------


async def test_a_jobs_pr_lands_on_the_issues_case_once(recorder, service, seed):
    issue_id = await seed.issue("[ENG-50] t")
    job_id = await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), job_id)
    url = "https://github.com/acme/widgets/pull/42"

    assert await recorder.pr_opened(issue_id=issue_id, job_id=job_id, pr_url=url) == case_id
    assert await recorder.pr_opened(issue_id=issue_id, job_id=job_id, pr_url=url) == case_id

    case = await service.get(case_id)
    assert case["stage"] == "pr_open"
    assert [(p["url"], p["number"], p["job_id"], p["state"]) for p in case["prs"]] == [(url, 42, job_id, "open")]
    assert (await service.find_by_pr_url(url))["id"] == case_id


async def test_a_pr_url_naming_a_renamed_repo_is_stored_under_the_current_slug(recorder, service, seed, monkeypatch):
    monkeypatch.setattr("embry0.storage.repo_renames.REPO_RENAMES", {"acme/old": "acme/new"})
    issue_id = await seed.issue("[ENG-50] t")
    job_id = await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), job_id)
    await recorder.pr_opened(issue_id=issue_id, job_id=job_id, pr_url="https://github.com/acme/old/pull/7")
    assert [p["url"] for p in (await service.get(case_id))["prs"]] == ["https://github.com/acme/new/pull/7"]


async def test_a_pr_of_an_issue_without_a_case_records_nothing(recorder, cases_repo, seed):
    issue_id = await seed.issue("plain")
    job_id = await seed.job(issue_id)
    assert await recorder.pr_opened(issue_id=issue_id, job_id=job_id, pr_url="https://github.com/a/b/pull/1") is None
    assert await recorder.pr_opened(issue_id=None, job_id=job_id, pr_url="https://github.com/a/b/pull/1") is None


# ---- fail-open ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.finding_recorded(
            repo=REPO, title="t", watcher_run_id="wr-1", fingerprint=None, severity=None, tracker=None
        ),
        lambda r: r.deferred_finding_filed("hc-1", TrackerRef("linear", "ENG-1"), watcher_run_id="wr-1"),
        lambda r: r.deferred_finding_dropped("hc-1", "why"),
        lambda r: r.finding_recurred(fingerprint="fp", repo=REPO, watcher_run_id="wr-1"),
        lambda r: r.draft_released(TrackerRef("linear", "ENG-1"), state="canceled"),
        lambda r: r.job_dispatched({"id": "iss-1", "tracker": "linear", "tracker_key": "ENG-1"}, "job-1"),
        lambda r: r.pr_opened(issue_id="iss-1", job_id="job-1", pr_url="https://github.com/a/b/pull/1"),
    ],
)
async def test_no_ledger_failure_ever_reaches_the_caller_and_none_is_silent(call):
    broken = MagicMock()
    for name in (
        "open_case", "attach_tracker", "close", "find_by_fingerprint", "find_by_tracker",
        "find_by_issue", "find_by_pr_url", "record_recurrence", "attach_job", "attach_pr",
    ):  # fmt: skip
        setattr(broken, name, AsyncMock(side_effect=RuntimeError("ledger down")))

    with structlog.testing.capture_logs() as logs:
        result = await call(CaseRecorder(broken))

    assert not result
    (entry,) = [e for e in logs if e["event"] == "healing_case_write_failed"]
    assert entry["log_level"] == "error"
    # ... and says which thing to repair.
    assert {"watcher_run_id", "case_id", "tracker_key", "issue_id", "fingerprint"} & set(entry)


async def test_a_missing_job_row_is_logged_not_raised(recorder, seed):
    issue_id = await seed.issue("[ENG-50] t")
    with structlog.testing.capture_logs() as logs:
        assert await recorder.job_dispatched(_issue(issue_id, "ENG-50"), "job-does-not-exist") is None
    assert [e["action"] for e in logs if e["event"] == "healing_case_write_failed"] == ["job_dispatched"]


# ---- a PR's open time and its spelling (review of #340 items 7 and 8) ---------


async def test_a_prs_opened_at_is_the_forges_not_the_moment_the_job_ended(recorder, service, seed, pr_times):
    issue_id = await seed.issue("[ENG-50] t")
    job_id = await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), job_id)
    url = "https://github.com/acme/widgets/pull/42"
    pr_times.answers[url] = OPENED

    await recorder.pr_opened(issue_id=issue_id, job_id=job_id, pr_url=url, job_started_at=datetime.now(UTC))

    (pr,) = (await service.get(case_id))["prs"]
    assert pr["opened_at"] == OPENED  # not "now", which reads as authoritative and is late
    assert pr_times.asked == [url]


async def test_a_forge_that_cannot_say_falls_back_to_the_job_start_and_says_so(recorder, service, seed):
    issue_id = await seed.issue("[ENG-50] t")
    job_id = await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), job_id)
    started = datetime.now(UTC) - timedelta(minutes=30)

    with structlog.testing.capture_logs() as logs:
        await recorder.pr_opened(
            issue_id=issue_id, job_id=job_id, pr_url="https://github.com/acme/widgets/pull/9", job_started_at=started
        )

    (pr,) = (await service.get(case_id))["prs"]
    # A bound that is never LATER than the truth: a real forge event after it
    # can still be told apart from a redelivery.
    assert pr["opened_at"] == started
    (warned,) = [e for e in logs if e["event"] == "healing_pr_open_time_not_from_forge"]
    assert warned["fallback"] == "job_start"


async def test_with_no_time_at_all_the_gap_is_still_reported(recorder, service, seed):
    issue_id = await seed.issue("[ENG-50] t")
    job_id = await seed.job(issue_id)
    await recorder.job_dispatched(_issue(issue_id, "ENG-50"), job_id)
    with structlog.testing.capture_logs() as logs:
        await recorder.pr_opened(issue_id=issue_id, job_id=job_id, pr_url="https://github.com/acme/widgets/pull/9")
    (warned,) = [e for e in logs if e["event"] == "healing_pr_open_time_not_from_forge"]
    assert warned["fallback"] == "record_time"


async def test_a_pr_stored_under_the_old_slug_is_found_by_the_new_one(recorder, service, seed, monkeypatch):
    # Nothing rewrites stored URLs after a rename, so a row written before
    # it keeps the old slug and both spellings are live.
    monkeypatch.setattr("embry0.storage.repo_renames.REPO_RENAMES", {"acme/old": "acme/new"})
    old = "https://github.com/acme/old/pull/7"
    new = "https://github.com/acme/new/pull/7"
    issue_id = await seed.issue("[ENG-50] t")
    job_id = await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), job_id)
    await service.attach_pr(case_id, url=old, number=7)

    assert await recorder.pr_opened(issue_id=issue_id, job_id=job_id, pr_url=new) == case_id

    prs = (await service.get(case_id))["prs"]
    assert [p["url"] for p in prs] == [old]  # one PR, kept under the spelling it is stored by
    assert prs[0]["job_id"] == job_id


async def test_a_pr_stored_under_the_new_slug_is_found_by_the_old_one(recorder, service, seed, monkeypatch):
    monkeypatch.setattr("embry0.storage.repo_renames.REPO_RENAMES", {"acme/old": "acme/new"})
    old = "https://github.com/acme/old/pull/8"
    new = "https://github.com/acme/new/pull/8"
    issue_id = await seed.issue("[ENG-50] t")
    job_id = await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "ENG-50"), job_id)
    await service.attach_pr(case_id, url=new, number=8)

    assert await recorder.pr_opened(issue_id=issue_id, job_id=job_id, pr_url=old) == case_id

    assert [p["url"] for p in (await service.get(case_id))["prs"]] == [new]


async def test_a_renamed_pr_of_another_case_is_never_stolen(recorder, service, seed, monkeypatch):
    # The variant lookup must find the PR's OWN case, not any case.
    monkeypatch.setattr("embry0.storage.repo_renames.REPO_RENAMES", {"acme/old": "acme/new"})
    old = "https://github.com/acme/old/pull/11"
    new = "https://github.com/acme/new/pull/11"
    first_issue = await seed.issue("[ENG-50] a")
    owner = await recorder.job_dispatched(_issue(first_issue, "ENG-50"), await seed.job(first_issue))
    await service.attach_pr(owner, url=old, number=11)
    other_issue = await seed.issue("[ENG-51] b")
    other_job = await seed.job(other_issue)
    other = await recorder.job_dispatched(_issue(other_issue, "ENG-51"), other_job)

    # A job of ANOTHER case reports the same PR under the new spelling: it
    # belongs to the case that owns it, and no second row appears.
    assert await recorder.pr_opened(issue_id=other_issue, job_id=other_job, pr_url=new) == owner
    assert [p["url"] for p in (await service.get(owner))["prs"]] == [old]
    assert (await service.get(other))["prs"] == []


# ---- #306: the case origin a job's issue belongs to -----------------------------


async def test_case_origin_reads_the_issues_case(recorder, service, seed):
    issue_id = await seed.issue("[ENG-60] t")
    job_id = await seed.job(issue_id)
    await recorder.job_dispatched(_issue(issue_id, "ENG-60"), job_id)
    assert await recorder.case_origin(issue_id) == "human"
    assert await recorder.case_origin(await seed.issue("no case")) is None


async def test_case_origin_is_fail_open():
    cases = MagicMock(find_by_issue=AsyncMock(side_effect=RuntimeError("db down")))
    assert await CaseRecorder(cases).case_origin("iss-1") is None


# ---- #344: PR-comment follow-ups join the case of their PR ----------------------


async def test_a_pr_follow_up_joins_the_case_whose_pr_another_job_attached(recorder, service, seed):
    issue_id = await seed.issue("[ENG-70] t")
    first = await seed.job(issue_id)
    case_id = await recorder.job_dispatched(_issue(issue_id, "ENG-70"), first)
    url = "https://github.com/acme/widgets/pull/70"
    await recorder.pr_opened(issue_id=issue_id, job_id=first, pr_url=url)
    follow_up = await seed.job()  # issue-less, as comment dispatch creates it

    assert await recorder.pr_followup_dispatched(pr_url=url, job_id=follow_up) == case_id
    assert await recorder.pr_followup_dispatched(pr_url=url, job_id=follow_up) == case_id  # idempotent

    case = await service.get(case_id)
    assert {j["job_id"] for j in case["jobs"]} == {first, follow_up}, "the follow-up job is missing from the case"
    assert case["stage"] == "pr_open", "linking a follow-up moved the case backwards"
    assert [p["job_id"] for p in case["prs"]] == [first], "the follow-up took over the PR"


async def test_a_follow_up_on_a_pr_with_no_case_records_nothing(recorder, cases_repo, seed):
    job_id = await seed.job()
    with structlog.testing.capture_logs() as logs:
        got = await recorder.pr_followup_dispatched(pr_url="https://github.com/acme/widgets/pull/71", job_id=job_id)
    assert got is None
    assert (await cases_repo.list_page())[1] == 0
    assert [e["log_level"] for e in logs if e["event"] == "healing_case_none_for_pr_followup"] == ["debug"]
