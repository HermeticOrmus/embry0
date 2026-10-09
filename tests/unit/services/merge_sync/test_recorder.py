"""DeliveryRecorder — matching, delivery_state, status, issue, ledger (#279, #280)."""

import random
from datetime import timedelta

import pytest
from structlog.testing import capture_logs

from embry0.orchestration.branch_names import job_branch_name, shared_branch_name
from embry0.services.merge_sync.facts import fact_from_github
from embry0.services.merge_sync.recorder import aligned_status

from .conftest import REPO, T0, pr_object

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

URL = f"https://github.com/{REPO}/pull/7"
MERGED_AT = T0 + timedelta(days=1)


def _events(logs, event):
    return [entry for entry in logs if entry["event"] == event]


def fact(**kwargs):
    built = fact_from_github(pr_object(**kwargs))
    assert built is not None
    return built


# ---------------------------------------------------------------- status rule


@pytest.mark.parametrize(
    ("status", "delivery", "expected"),
    [
        ("completed", "merged", "pr_merged"),
        ("completed", "closed", "pr_closed"),
        ("completed", "pr_open", None),
        ("pr_closed", "merged", "pr_merged"),
        ("pr_closed", "pr_open", "completed"),
        ("pr_merged", "merged", None),
        ("failed", "merged", None),
        ("cancelled", "merged", None),
        ("partial", "closed", None),
        ("running", "merged", None),
        ("expired", "merged", None),
    ],
)
async def test_status_follows_delivery_only_where_its_meaning_allows(status, delivery, expected):
    assert aligned_status(status, delivery) == expected


# ------------------------------------------------------------------- matching


async def test_storing_a_pr_url_opens_delivery(seed):
    job = await seed.job(pr_url=URL)
    assert await seed.delivery(job) == ("completed", "pr_open")


@pytest.mark.parametrize("status", ["failed", "cancelled", "partial", "expired", "completed", "running"])
async def test_a_merge_is_recorded_on_a_job_in_any_state(seed, recorder, status):
    job = await seed.job(status=status, pr_url=URL)

    result = await recorder.record(fact(merged=MERGED_AT), source="webhook")

    row = await seed.row(job)
    assert (row["delivery_state"], row["delivery_state_at"]) == ("merged", MERGED_AT)
    # only `completed` has a status that speaks about the PR; the rest keep theirs
    assert row["status"] == ("pr_merged" if status == "completed" else status)
    assert [c.job_id for c in result.changes] == [job]


async def test_a_pr_is_matched_by_its_stored_url_across_a_repo_rename(seed, recorder, db_with_migrations, monkeypatch):
    monkeypatch.setattr(
        "embry0.storage.repo_renames.REPO_RENAMES",
        {"acme/old": "acme/new"},
    )
    job = await seed.job(status="failed", repo="acme/new")
    # a row a future rename has not been migrated in yet
    await db_with_migrations.execute(
        "UPDATE jobs SET pr_url = 'https://github.com/acme/old/pull/418', delivery_state = 'pr_open' WHERE job_id = $1",
        job,
    )
    merged = fact(number=418, repo="acme/new", ref="some/human-branch", merged=MERGED_AT)

    await recorder.record(merged, source="sweep")

    assert await seed.delivery(job) == ("failed", "merged")


async def test_a_pr_opened_by_a_human_from_a_failed_jobs_branch_is_found_by_branch(seed, recorder):
    """The 2026-09-20 gap: `embry0/unknown-*` branches, and PRs nobody stored a URL for."""
    job = await seed.job(status="failed", task="Backend: idle-timer auto-return for tasks")
    ref = "embry0/unknown-backend-idle-timer-auto-retur"

    result = await recorder.record(fact(ref=ref, merged=MERGED_AT), source="sweep")

    row = await seed.row(job)
    assert (row["status"], row["delivery_state"], row["pr_url"]) == ("failed", "merged", URL)
    assert result.changes[0].matched_by == "branch" and result.changes[0].pr_url_linked


async def test_branch_matching_covers_issue_branches_and_follow_up_refs(seed, recorder):
    issue = await seed.issue()
    own = await seed.job(status="failed", issue_id=issue, task="[ENG-1244] [watcher] Fix acme 500s")
    ref = f"embry0/{issue[:12]}-eng-1244-watcher-fix-acme"
    follow_up = await seed.job(status="completed", task="PR follow-up", context={"type": "git", "ref": ref})

    await recorder.record(fact(ref=ref, merged=MERGED_AT), source="sweep")

    assert await seed.delivery(own) == ("failed", "merged")
    assert await seed.delivery(follow_up) == ("pr_merged", "merged")


async def test_branch_matching_never_crosses_repos_forks_kinds_or_time(seed, recorder):
    task = "Fix the thing"
    other_repo = await seed.job(status="failed", task=task, repo="acme/gadgets")
    analysis = await seed.job(status="failed", task=task, job_kind="analysis")
    created_later = await seed.job(status="failed", task=task, created=T0 + timedelta(hours=2))
    linked_elsewhere = await seed.job(status="failed", task=task, pr_url=f"https://github.com/{REPO}/pull/3")
    candidate = await seed.job(status="failed", task=task)

    from_fork = fact(merged=MERGED_AT, head_repo="mallory/widgets")
    assert (await recorder.record(from_fork, source="sweep")).matched_jobs == []

    result = await recorder.record(fact(merged=MERGED_AT), source="sweep")

    assert result.matched_jobs == [candidate]
    for untouched in (other_repo, analysis, created_later):
        assert (await seed.delivery(untouched))[1] == "none"
    assert (await seed.delivery(linked_elsewhere))[1] == "pr_open"


async def test_an_unknown_pr_matches_nothing_and_writes_nothing(seed, recorder):
    await seed.job(status="failed", task="Something else entirely")
    result = await recorder.record(fact(merged=MERGED_AT), source="webhook")
    assert not result.matched and result.changes == []


# ------------------------------------------------------- ordering, redelivery


async def test_merged_delivered_twice_is_one_change(seed, recorder):
    job = await seed.job(pr_url=URL)
    first = await recorder.record(fact(merged=MERGED_AT), source="webhook", event_key="d-1")
    before = (await seed.row(job))["updated_at"]
    again = await recorder.record(fact(merged=MERGED_AT), source="webhook", event_key="d-1")
    swept = await recorder.record(fact(merged=MERGED_AT), source="sweep")

    assert len(first.changes) == 1 and again.changes == [] and swept.changes == []
    assert (await seed.row(job))["updated_at"] == before  # nothing was rewritten


async def test_closed_then_reopened_then_merged_in_any_order(seed, recorder):
    closed = fact(closed=T0 + timedelta(hours=1))
    reopened = fact(updated=T0 + timedelta(hours=2))
    merged = fact(merged=T0 + timedelta(hours=3))

    job = await seed.job(pr_url=URL)
    await recorder.record(closed, source="webhook")
    assert await seed.delivery(job) == ("pr_closed", "closed")
    await recorder.record(reopened, source="webhook")
    assert await seed.delivery(job) == ("completed", "pr_open")
    await recorder.record(closed, source="webhook")  # a late redelivery of the older close
    assert await seed.delivery(job) == ("completed", "pr_open")
    await recorder.record(merged, source="webhook")
    assert await seed.delivery(job) == ("pr_merged", "merged")
    for stale in (closed, reopened):
        await recorder.record(stale, source="webhook")
    assert await seed.delivery(job) == ("pr_merged", "merged")


@pytest.mark.parametrize("seed_number", range(6))
async def test_random_delivery_orders_end_at_what_the_forge_says(seed, recorder, healing, seed_number):
    rng = random.Random(seed_number)
    issue = await seed.issue()
    job = await seed.job(status=rng.choice(["completed", "failed", "cancelled"]), pr_url=URL, issue_id=issue)
    case = await healing.open_case(repo=REPO, origin="human", title="Fix the thing")
    await healing.attach_issue(case["id"], issue_id=issue)
    await healing.attach_job(case["id"], job_id=job)

    when = T0
    truth = [fact()]
    for _ in range(rng.randint(0, 2)):
        when += timedelta(hours=1)
        truth.append(fact(closed=when))
        when += timedelta(hours=1)
        truth.append(fact(updated=when))
    ending = rng.choice(["open", "closed", "merged"])
    when += timedelta(hours=1)
    if ending == "closed":
        truth.append(fact(closed=when))
    elif ending == "merged":
        truth.append(fact(merged=when))

    deliveries = truth + [rng.choice(truth) for _ in range(4)]
    rng.shuffle(deliveries)
    for number, delivered in enumerate(deliveries):
        await recorder.record(delivered, source=rng.choice(["webhook", "webhook", "sweep"]), event_key=None)
        del number
    # a sweep always ends a real sequence: it reports the forge's current answer
    await recorder.record(truth[-1], source="sweep")

    row = await seed.row(job)
    assert row["delivery_state"] == truth[-1].state.value
    detail = await healing.get(case["id"])
    assert [p["state"] for p in detail["prs"]] == [
        {"pr_open": "open"}.get(truth[-1].state.value, truth[-1].state.value)
    ]
    assert detail["stage"] == {"pr_open": "pr_open", "merged": "merged", "closed": "accepted"}[truth[-1].state.value]


# ---------------------------------------------------------------------- issue


async def test_the_issue_is_closed_once_when_the_merge_is_first_recorded(seed, recorder, db_with_migrations):
    issue = await seed.issue()
    await seed.job(status="failed", pr_url=URL, issue_id=issue)

    first = await recorder.record(fact(merged=MERGED_AT), source="sweep")
    assert first.issues_closed == [issue]
    assert await db_with_migrations.fetchval("SELECT status FROM issues WHERE id = $1", issue) == "closed"

    assert (await recorder.record(fact(merged=MERGED_AT), source="sweep")).issues_closed == []


async def test_an_issue_that_cannot_be_closed_does_not_lose_the_delivery_fact(seed, recorder, db_with_migrations):
    issue = await seed.issue()
    await db_with_migrations.execute("UPDATE issues SET status = 'cancelled' WHERE id = $1", issue)
    job = await seed.job(status="failed", pr_url=URL, issue_id=issue)

    result = await recorder.record(fact(merged=MERGED_AT), source="webhook")

    assert result.issues_closed == []
    assert await seed.delivery(job) == ("failed", "merged")


# --------------------------------------------------------------------- ledger


async def _case_with_open_pr(seed, healing, *, status="failed"):
    issue = await seed.issue()
    job = await seed.job(status=status, pr_url=URL, issue_id=issue)
    case = await healing.open_case(repo=REPO, origin="watcher", title="Fix the thing")
    await healing.attach_issue(case["id"], issue_id=issue)
    await healing.attach_job(case["id"], job_id=job)
    await healing.attach_pr(case["id"], url=URL, job_id=job)
    return case["id"], job


async def test_a_merge_moves_the_case_with_the_forges_merge_time_and_the_embry0_path(seed, recorder, healing):
    """The three backfilled cases stuck at pr_open (#418, #419, #442): failed job, merged PR."""
    case_id, _ = await _case_with_open_pr(seed, healing)

    result = await recorder.record(fact(merged=MERGED_AT), source="sweep")

    case = await healing.get(case_id)
    assert (case["stage"], case["resolution_path"], case["merged_at"]) == ("merged", "embry0", MERGED_AT)
    assert case["merge_sha"] == "m" * 40
    assert (result.case_id, result.case_outcome) == (case_id, "applied")
    # the same observation again is dropped by its key
    assert (await recorder.record(fact(merged=MERGED_AT), source="sweep")).case_outcome == "duplicate"


async def test_a_pr_the_case_owns_without_any_embry0_job_is_out_of_band(seed, recorder, healing):
    case = await healing.open_case(repo=REPO, origin="watcher", title="Fix the thing")
    await healing.attach_pr(case["id"], url=URL)  # attached by ticket reference (#282)

    await recorder.record(fact(ref="human/fix", merged=MERGED_AT), source="webhook", event_key="d-9")

    got = await healing.get(case["id"])
    assert (got["stage"], got["resolution_path"]) == ("merged", "out_of_band")


async def test_a_close_without_merge_sends_the_case_back_and_a_reopen_forward(seed, recorder, healing):
    case_id, _ = await _case_with_open_pr(seed, healing, status="completed")

    await recorder.record(fact(closed=T0 + timedelta(hours=1)), source="webhook", event_key="d-1")
    assert (await healing.get(case_id))["stage"] == "accepted"

    await recorder.record(fact(updated=T0 + timedelta(hours=2)), source="webhook", event_key="d-2")
    assert (await healing.get(case_id))["stage"] == "pr_open"

    # the older close, redelivered under a new delivery id, changes nothing
    await recorder.record(fact(closed=T0 + timedelta(hours=1)), source="webhook", event_key="d-3")
    assert (await healing.get(case_id))["stage"] == "pr_open"


async def test_an_opened_pr_is_attached_to_the_case_of_the_jobs_issue(seed, recorder, healing):
    issue = await seed.issue()
    job = await seed.job(status="running", issue_id=issue, task="Fix the thing")
    case = await healing.open_case(repo=REPO, origin="human", title="Fix the thing")
    await healing.attach_issue(case["id"], issue_id=issue)

    await recorder.record(fact(ref=f"embry0/{issue[:12]}-fix-the-thing"), source="webhook", event_key="d-1")

    detail = await healing.get(case["id"])
    assert detail["stage"] == "pr_open"
    assert [(p["url"], p["job_id"], p["head_sha"]) for p in detail["prs"]] == [(URL, job, "h" * 40)]


async def test_a_ledger_failure_never_loses_the_job_fact(seed, delivery_repo, db_with_migrations):
    from embry0.services.merge_sync.recorder import DeliveryRecorder
    from embry0.storage.repositories.issues import IssuesRepository

    class Broken:
        async def find_by_pr_url(self, url):
            raise RuntimeError("ledger down")

    job = await seed.job(status="failed", pr_url=URL)
    recorder = DeliveryRecorder(delivery_repo, IssuesRepository(db_with_migrations), Broken())

    await recorder.record(fact(merged=MERGED_AT), source="webhook")

    assert await seed.delivery(job) == ("failed", "merged")


async def test_a_dry_run_reports_the_same_changes_and_writes_nothing(seed, recorder, healing):
    case_id, job = await _case_with_open_pr(seed, healing)

    result = await recorder.record(fact(merged=MERGED_AT), source="backfill", dry_run=True)

    assert [(c.job_id, c.delivery_from, c.delivery_to) for c in result.changes] == [(job, "pr_open", "merged")]
    assert await seed.delivery(job) == ("failed", "pr_open")
    assert (await healing.get(case_id))["stage"] == "pr_open"


async def test_a_follow_up_job_created_after_the_pr_still_belongs_to_it(seed, recorder):
    owner = await seed.job(status="failed", pr_url=URL)
    follow_up = await seed.job(
        status="completed",
        task="PR follow-up",
        context={"type": "git", "ref": "embry0/unknown-fix-the-thing"},
        created=T0 + timedelta(hours=6),
    )

    await recorder.record(fact(merged=MERGED_AT), source="sweep")

    assert await seed.delivery(owner) == ("failed", "merged")
    assert await seed.delivery(follow_up) == ("pr_merged", "merged")


async def test_an_earlier_attempt_with_the_same_branch_name_is_not_credited_once_a_job_claims_the_pr(seed, recorder):
    """Task slugs repeat: `embry0/unknown-<first 30 chars>` is shared by every retry."""
    earlier = await seed.job(status="cancelled", created=T0 - timedelta(hours=3))
    owner = await seed.job(status="completed", pr_url=URL)

    result = await recorder.record(fact(closed=MERGED_AT), source="sweep")

    assert result.matched_jobs == [owner]
    assert await seed.delivery(earlier) == ("cancelled", "none")


async def test_a_pr_from_a_jobs_own_branch_is_credited_to_that_job_alone(seed, recorder):
    """#478: a job dispatched again after an earlier one left the derived branch on
    origin pushes to `<derived>-<job hex>`. That name is the job's own, so the PR
    goes to it, not to the newest job that merely shares the derived name."""
    issue = await seed.issue()
    task = "[ENG-2435] [watcher] Fix vendor display names"
    await seed.job(status="cancelled", issue_id=issue, task=task, created=T0 - timedelta(hours=3))
    again = await seed.job(status="failed", issue_id=issue, task=task, created=T0 - timedelta(hours=2))
    await seed.job(status="failed", issue_id=issue, task=task, created=T0 - timedelta(hours=1))
    own = f"{shared_branch_name(issue_id=issue, task=task)}-{again.removeprefix('job-')[:8]}"

    result = await recorder.record(fact(ref=own, merged=MERGED_AT), source="sweep")

    assert result.matched_jobs == [again], f"the PR from {own} belongs to {again}: {result.matched_jobs}"
    assert await seed.delivery(again) == ("failed", "merged")


async def test_a_pr_from_an_issueless_jobs_branch_is_credited_to_that_job_not_a_newer_sibling(seed, recorder):
    """#558: an issue-less job pushes the name its developer derives, which carries its
    job id. A newer job whose task begins the same way did not push it, even though
    the newest-before-the-PR rule would pick that job for a shared name."""
    task = "Deploy proof job 2026-10-08: append one proof line"
    first = await seed.job(status="failed", task=task, created=T0 - timedelta(hours=2))
    await seed.job(status="failed", task=task + " (second)", created=T0 - timedelta(hours=1))
    pushed = job_branch_name(issue_id=None, task=task, job_id=first)

    result = await recorder.record(fact(ref=pushed, merged=MERGED_AT), source="sweep")

    assert result.matched_jobs == [first], f"the PR from {pushed} belongs to {first}: {result.matched_jobs}"
    assert await seed.delivery(first) == ("failed", "merged")


async def test_one_pr_never_fans_out_across_jobs_that_merely_share_a_derived_branch(seed, recorder):
    """The live rows hold 13 colliding (repo, branch) names — the worst is
    `embry0/unknown-qa-run-on-acme-backend-nightly`, derived by 37 unlinked
    jobs created before #558. Only one of them can have pushed the branch this
    PR came from."""
    task = "QA run on acme/backend nightly"
    others = [
        await seed.job(status=("cancelled" if n % 2 else "failed"), task=task, created=T0 - timedelta(hours=40 - n))
        for n in range(36)
    ]
    newest = await seed.job(status="failed", task=task, created=T0 - timedelta(minutes=1))
    ref = shared_branch_name(issue_id=None, task=task)

    result = await recorder.record(fact(ref=ref, merged=MERGED_AT), source="sweep")

    assert result.matched_jobs == [newest]
    assert [c.job_id for c in result.changes] == [newest]
    for untouched in others:
        row = await seed.row(untouched)
        assert (row["delivery_state"], row["pr_url"]) == ("none", None)


async def test_an_ambiguous_branch_credit_is_logged_not_made_silently(seed, recorder):
    task = "QA run on acme/backend nightly"
    await seed.job(status="failed", task=task, created=T0 - timedelta(hours=3))
    newest = await seed.job(status="failed", task=task, created=T0 - timedelta(hours=1))

    with capture_logs() as logs:
        await recorder.record(fact(ref=shared_branch_name(issue_id=None, task=task), merged=MERGED_AT), source="sweep")

    ambiguous = _events(logs, "merge_sync_branch_match_ambiguous")
    assert len(ambiguous) == 1
    assert (ambiguous[0]["candidates"], ambiguous[0]["credited"]) == (2, newest)


async def test_a_single_branch_candidate_is_not_logged_as_ambiguous(seed, recorder):
    only = await seed.job(status="failed", task="Fix the thing")

    with capture_logs() as logs:
        result = await recorder.record(fact(merged=MERGED_AT), source="sweep")

    assert result.matched_jobs == [only]
    assert _events(logs, "merge_sync_branch_match_ambiguous") == []


async def test_every_follow_up_on_the_branch_is_still_credited(seed, recorder):
    """A `context.ref` job was HANDED the branch, so it is not a guess."""
    ref = "embry0/unknown-fix-the-thing"
    pusher = await seed.job(status="failed", task="Fix the thing", created=T0 - timedelta(hours=1))
    follow_ups = [
        await seed.job(status="completed", task="PR follow-up", context={"ref": ref}, created=T0 + timedelta(hours=n))
        for n in (1, 2)
    ]

    result = await recorder.record(fact(ref=ref, merged=MERGED_AT), source="sweep")

    assert sorted(result.matched_jobs) == sorted([pusher, *follow_ups])


async def test_no_single_fact_may_rewrite_an_unbounded_number_of_rows(seed, recorder):
    """The cap is a backstop: follow-ups are explicit, but nothing in the
    schema stops a branch being handed to more jobs than a PR can have."""
    from embry0.services.merge_sync.recorder import MAX_BRANCH_MATCHES

    ref = "embry0/unknown-fix-the-thing"
    for n in range(MAX_BRANCH_MATCHES + 3):
        await seed.job(status="completed", task="PR follow-up", context={"ref": ref}, created=T0 + timedelta(hours=n))

    with capture_logs() as logs:
        result = await recorder.record(fact(ref=ref, merged=MERGED_AT), source="sweep")

    assert len(result.matched_jobs) == MAX_BRANCH_MATCHES
    assert _events(logs, "merge_sync_branch_match_capped")[0]["matched"] == MAX_BRANCH_MATCHES + 3


async def test_an_undated_fact_is_not_given_a_constant_event_key(seed, recorder, healing):
    """Every undated observation of one PR in one state would share a key, so
    the first would turn the rest into duplicates — new information included."""
    case_id, job = await _case_with_open_pr(seed, healing)
    undated = fact_from_github(
        {
            "html_url": URL,
            "number": 7,
            "state": "closed",
            "merged": True,
            "head": {"ref": "embry0/unknown-fix", "repo": {"full_name": REPO}},
            "base": {"repo": {"full_name": REPO}},
        }
    )
    assert undated is not None and undated.at is None

    first = await recorder.record(undated, source="sweep")
    assert first.case_outcome == "applied"

    # A later dated observation of the same merge must still be heard.
    second = await recorder.record(fact(merged=MERGED_AT), source="sweep")
    assert second.case_outcome != "duplicate"
    assert (await healing.get(case_id))["merge_sha"] == "m" * 40
    del job
