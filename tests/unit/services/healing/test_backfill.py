"""Backfill of historical watcher proposals into the ledger (#278).

The fixtures reproduce the shapes found in the live table on 2026-09-20
(30 proposals): a draft nobody accepted, a draft whose Linear id reconcile
had erased, and accepted drafts whose issue ran one or several jobs ending
failed / pr_merged / pr_closed. Analysis jobs carry no repo, so an
unaccepted proposal's repo can only come from WATCHER_REPO.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.api.app import _backfill_healing_cases
from embry0.config import Embry0Config
from embry0.services.healing.backfill import LOST_TRACKER_NOTE, UNVERIFIED_PR_NOTE, backfill_watcher_proposals
from embry0.services.healing.lifecycle import ResolutionPath, Stage
from embry0.services.healing.service import HealingCaseService
from embry0.storage.database import DatabasePool
from embry0.storage.repositories.healing_cases import HealingCasesRepository

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

WATCHED = "acme/backend"
T0 = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)


class Fixture:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db
        self._n = 0

    async def run(self, identifier: str | None, title: str | None, action: str = "proposed") -> str:
        self._n += 1
        run_id = f"wr-{self._n:04d}"
        await self.db.execute(
            """
            INSERT INTO watcher_runs (id, window_start, window_end, log_lines, action, detail,
                                      fingerprint, tracker, tracker_key, created_at)
            VALUES ($1, $2, $2, 40, $3, $4, $5, CASE WHEN $6::text IS NULL THEN NULL ELSE 'linear' END, $6, $2)
            """,
            run_id,
            T0 + timedelta(hours=self._n),
            action,
            title,
            f"fp-{self._n}",
            identifier,
        )
        return run_id

    async def issue(self, identifier: str, title: str, repo: str = WATCHED) -> str:
        issue_id = f"iss-{identifier.lower()}"
        await self.db.execute(
            """
            INSERT INTO issues (id, title, repo, status, tracker, tracker_key, created_at)
            VALUES ($1, $2, $3, 'closed', 'linear', $4, $5)
            """,
            issue_id,
            f"[{identifier}] [watcher] {title}",
            repo,
            identifier,
            T0 + timedelta(days=1),
        )
        return issue_id

    async def job(self, issue_id: str, status: str, pr_url: str | None, minutes: int) -> str:
        self._n += 1
        job_id = f"job-{self._n:04d}"
        created = T0 + timedelta(days=1, minutes=minutes)
        await self.db.execute(
            """
            INSERT INTO jobs (job_id, status, repo, task, issue_id, pr_url, job_kind,
                              created_at, finished_at, updated_at)
            VALUES ($1, $2, $3, 'fix', $4, $5, 'code', $6, $7, $8)
            """,
            job_id,
            status,
            WATCHED,
            issue_id,
            pr_url,
            created,
            created + timedelta(minutes=30),
            created + timedelta(hours=5),
        )
        return job_id


@pytest.fixture
def fx(db_with_migrations: DatabasePool) -> Fixture:
    return Fixture(db_with_migrations)


async def _case_for(cases_repo: HealingCasesRepository, run_id: str) -> dict:
    case = await cases_repo.find_by_watcher_run(run_id)
    assert case is not None
    detail = await cases_repo.get_detail(case["id"])
    assert detail is not None
    return detail


async def test_an_unaccepted_draft_becomes_a_tracked_case(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    run_id = await fx.run("ENG-1305", "Fix MsGraphFetcher RestClientException")
    report = await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert (report.proposals, report.created) == (1, 1)

    case = await _case_for(cases_repo, run_id)
    assert case["stage"] == Stage.TRACKED
    assert case["origin"] == "watcher"
    assert case["repo"] == WATCHED
    assert case["title"] == "Fix MsGraphFetcher RestClientException"
    assert case["fingerprint"] == "fp-1"
    assert (case["tracker"], case["tracker_key"]) == ("linear", "ENG-1305")
    assert case["issue_id"] is None
    assert case["note"] is None
    # The case is dated when the proposal was made, not when it was backfilled.
    assert case["detected_at"] == T0 + timedelta(hours=1)


async def test_a_lost_ticket_id_is_not_guessed(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    # An issue with a near-identical title exists; the backfill must not reach for it.
    await fx.issue("ENG-1099", "CircuitBreaker msGraphProxy stuck OPEN")
    run_id = await fx.run(None, "CircuitBreaker msGraphProxy stuck OPEN")
    report = await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert report.without_tracker == 1

    case = await _case_for(cases_repo, run_id)
    assert (case["tracker"], case["tracker_key"], case["issue_id"]) == (None, None, None)
    assert case["note"] == LOST_TRACKER_NOTE
    assert case["jobs"] == []
    # Reconcile only ever erased the id of a draft it had released, so this
    # one is known dead: closed, and never able to pin the proposal cap.
    assert (case["stage"], case["resolution_path"]) == (Stage.CLOSED, ResolutionPath.CANCELLED)


def _prs(case: dict) -> list[tuple]:
    return [(p["number"], p["state"], p["job_id"]) for p in case["prs"]]


async def test_a_pr_the_database_cannot_judge_stays_open_and_says_so(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    # Live shape of #442/#418/#419: merged on GitHub, but the job row says
    # `failed`, so nothing in the DB proves it. Not guessed: merge sync
    # (#279) will record_merge it.
    run_id = await fx.run("ENG-1244", "Fix Apollo grounding 422s")
    issue_id = await fx.issue("ENG-1244", "Fix Apollo grounding 422s")
    job_id = await fx.job(issue_id, "failed", "https://github.com/acme/backend/pull/442", minutes=0)
    report = await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert report.unverified_prs == 1

    case = await _case_for(cases_repo, run_id)
    assert case["stage"] == Stage.PR_OPEN
    assert case["issue_id"] == issue_id
    assert [j["job_id"] for j in case["jobs"]] == [job_id]
    assert _prs(case) == [(442, "open", job_id)]
    assert case["note"] == UNVERIFIED_PR_NOTE
    assert (case["merge_sha"], case["resolution_path"]) == (None, None)


async def test_a_merged_job_among_retries_marks_the_case_merged(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    pr = "https://github.com/acme/backend/pull/445"
    run_id = await fx.run("ENG-1252", "Fix dropped Graph notifications")
    issue_id = await fx.issue("ENG-1252", "Fix dropped Graph notifications")
    jobs = [
        await fx.job(issue_id, "failed", pr, minutes=0),
        await fx.job(issue_id, "pr_merged", pr, minutes=10),
        await fx.job(issue_id, "failed", pr, minutes=20),
    ]
    await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)

    case = await _case_for(cases_repo, run_id)
    assert case["stage"] == Stage.MERGED
    assert case["resolution_path"] == ResolutionPath.EMBRY0
    assert [j["job_id"] for j in case["jobs"]] == jobs
    # Three jobs, one PR — credited to the job that proves the merge.
    assert _prs(case) == [(445, "merged", jobs[1])]
    assert case["note"] is None
    # The merge commit was never stored anywhere; it is left empty, not invented.
    assert (case["merge_sha"], case["prs"][0]["merge_sha"]) == (None, None)
    # Dated by when the job row flipped to pr_merged.
    assert case["merged_at"] == T0 + timedelta(days=1, minutes=10, hours=5)
    assert case["healed_verdict"] == "pending"


async def test_a_pr_the_database_knows_was_closed_is_closed(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    # Live shape of #366 (RAV-1066): the job row says pr_closed.
    run_id = await fx.run("ENG-1066", "msGraphProxy circuit breaker")
    issue_id = await fx.issue("ENG-1066", "msGraphProxy circuit breaker")
    job_id = await fx.job(issue_id, "pr_closed", "https://github.com/acme/backend/pull/366", minutes=0)
    await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    case = await _case_for(cases_repo, run_id)
    assert case["stage"] == Stage.ACCEPTED
    assert _prs(case) == [(366, "closed", job_id)]
    assert case["prs"][0]["closed_at"] == T0 + timedelta(days=1, hours=5)
    assert (case["merged_at"], case["note"]) == (None, None)
    assert case["pr_open_at"] is not None


async def test_a_superseded_pr_and_its_replacement_both_survive(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    run_id = await fx.run("ENG-1066", "msGraphProxy circuit breaker")
    issue_id = await fx.issue("ENG-1066", "msGraphProxy circuit breaker")
    first = await fx.job(issue_id, "pr_closed", "https://github.com/acme/backend/pull/366", minutes=0)
    second = await fx.job(issue_id, "pr_merged", "https://github.com/acme/backend/pull/437", minutes=10)
    await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    case = await _case_for(cases_repo, run_id)
    assert case["stage"] == Stage.MERGED
    assert _prs(case) == [(366, "closed", first), (437, "merged", second)]


async def test_only_proposals_get_cases(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    for action in ("skipped_quiet", "no_issue", "skipped_duplicate", "error"):
        await fx.run(None, "noise", action=action)
    report = await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert (report.proposals, report.created) == (0, 0)
    assert (await cases_repo.list_page())[1] == 0


async def test_the_issue_repo_wins_over_the_default(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    run_id = await fx.run("ENG-7", "elsewhere")
    await fx.issue("ENG-7", "elsewhere", repo="acme/other")
    await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert (await _case_for(cases_repo, run_id))["repo"] == "acme/other"


async def test_a_proposal_with_no_knowable_repo_is_skipped(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    await fx.run("ENG-8", "orphan")
    report = await backfill_watcher_proposals(cases_repo, default_repo="")
    assert (report.created, report.skipped_no_repo) == (0, 1)
    # Setting WATCHER_REPO later lets the next pass pick it up.
    report = await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert report.created == 1


async def test_a_second_pass_changes_nothing(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository, db_with_migrations: DatabasePool
) -> None:
    await fx.run("ENG-1305", "unaccepted")
    await fx.run(None, "lost")
    issue_id = await fx.issue("ENG-1252", "merged")
    await fx.run("ENG-1252", "merged")
    await fx.job(issue_id, "pr_merged", "https://github.com/acme/backend/pull/445", minutes=0)
    other = await fx.issue("ENG-1066", "closed then unverified")
    await fx.run("ENG-1066", "closed then unverified")
    await fx.job(other, "pr_closed", "https://github.com/acme/backend/pull/366", minutes=0)
    await fx.job(other, "failed", "https://github.com/acme/backend/pull/419", minutes=10)

    first = await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert (first.proposals, first.created, first.existing) == (4, 4, 0)

    async def snapshot() -> list:
        return [
            await db_with_migrations.fetch("SELECT * FROM healing_cases ORDER BY id"),
            await db_with_migrations.fetch("SELECT * FROM healing_case_jobs ORDER BY case_id, job_id"),
            await db_with_migrations.fetch("SELECT * FROM healing_case_prs ORDER BY id"),
            await db_with_migrations.fetch("SELECT * FROM healing_case_events ORDER BY id"),
        ]

    before = await snapshot()
    second = await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert (second.proposals, second.created, second.existing) == (4, 0, 4)
    assert await snapshot() == before


async def test_a_case_that_moved_on_is_left_alone(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    run_id = await fx.run("ENG-1305", "unaccepted")
    await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    case = await _case_for(cases_repo, run_id)
    await service.close(case["id"], resolution_path=ResolutionPath.CANCELLED)

    report = await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert report.existing == 1
    assert (await _case_for(cases_repo, run_id))["stage"] == Stage.CLOSED


async def test_a_reopened_case_is_not_rewound_to_its_first_attempt(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    run_id = await fx.run("ENG-1252", "merged")
    issue_id = await fx.issue("ENG-1252", "merged")
    await fx.job(issue_id, "pr_merged", "https://github.com/acme/backend/pull/445", minutes=0)
    await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    case = await _case_for(cases_repo, run_id)
    await service.record_verdict(case["id"], "not_healed", evidence={"after": 30})
    await service.reopen(case["id"], reason="still firing")

    await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert (await _case_for(cases_repo, run_id))["stage"] == Stage.ACCEPTED


async def test_a_job_that_appears_later_is_linked_on_the_next_pass(
    fx: Fixture, service: HealingCaseService, cases_repo: HealingCasesRepository
) -> None:
    # Until the live writers land (part 2 of #278), each restart catches the
    # ledger up with whatever the pipeline did since.
    run_id = await fx.run("ENG-1988", "Fix LazyInitializationException")
    await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    issue_id = await fx.issue("ENG-1988", "Fix LazyInitializationException")
    job_id = await fx.job(issue_id, "running", None, minutes=0)

    await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    case = await _case_for(cases_repo, run_id)
    assert case["stage"] == Stage.ACCEPTED
    assert [j["job_id"] for j in case["jobs"]] == [job_id]


async def test_the_startup_hook_uses_watcher_repo(
    fx: Fixture, cases_repo: HealingCasesRepository, db_with_migrations: DatabasePool
) -> None:
    run_id = await fx.run("ENG-1305", "unaccepted")
    await _backfill_healing_cases(db_with_migrations, Embry0Config(_env_file=None, watcher_repo=WATCHED))
    assert (await _case_for(cases_repo, run_id))["repo"] == WATCHED


async def test_the_startup_hook_is_fail_open() -> None:
    broken = MagicMock(fetch=AsyncMock(side_effect=RuntimeError("relation does not exist")))
    await _backfill_healing_cases(broken, Embry0Config(_env_file=None))


async def test_the_startup_hook_gives_up_after_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import embry0.api.app as app_module
    import embry0.services.healing.backfill as backfill_module

    async def hang(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(backfill_module, "backfill_watcher_proposals", hang)
    monkeypatch.setattr(app_module, "HEALING_BACKFILL_TIMEOUT_SECONDS", 0.05)
    await asyncio.wait_for(_backfill_healing_cases(MagicMock(), Embry0Config(_env_file=None)), timeout=5)


async def test_an_interrupted_pass_leaves_only_whole_cases(
    fx: Fixture,
    service: HealingCaseService,
    cases_repo: HealingCasesRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The startup timeout cancels the pass wherever it happens to be. A case
    # is one transaction, so what it leaves behind is complete or absent —
    # never an id-less case opened but not yet closed, pinning nothing but
    # lying about its state until the next restart.
    first = await fx.run("ENG-1305", "whole")
    lost = await fx.run(None, "lost id")
    real_close = HealingCaseService.close

    async def cancelled_mid_case(self: HealingCaseService, *args: object, **kwargs: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(HealingCaseService, "close", cancelled_mid_case)
    with pytest.raises(asyncio.CancelledError):
        await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert await cases_repo.find_by_watcher_run(first) is not None
    assert await cases_repo.find_by_watcher_run(lost) is None

    monkeypatch.setattr(HealingCaseService, "close", real_close)
    await backfill_watcher_proposals(cases_repo, default_repo=WATCHED)
    assert (await _case_for(cases_repo, lost))["stage"] == Stage.CLOSED
