"""MergeSyncSweep — the periodic reconcile, the backfill and the DB==GitHub check (#279)."""

from datetime import timedelta
from typing import Any

import httpx
import pytest

from embry0.services.merge_sync.sweep import MergeSyncSettings, MergeSyncSweep, parse_github_pr_url

from .conftest import REPO, T0, pr_object

MERGED_AT = T0 + timedelta(days=1)
NOW = T0 + timedelta(days=2)


class FakeGitHub:
    """Serves ``GET /repos/{slug}/pulls[/{n}]`` from a dict of PR objects.
    Refuses anything but GET: the sweep is read-only by construction."""

    def __init__(self, prs: list[dict[str, Any]], *, remaining: int = 4000, status: int = 200) -> None:
        self.prs = {(p["base"]["repo"]["full_name"], p["number"]): p for p in prs}
        self.remaining = remaining
        self.status = status
        self.calls: list[tuple[str, str | None, dict[str, Any] | None]] = []

    async def get(self, path: str, *, repo: str | None = None, params: dict[str, Any] | None = None) -> httpx.Response:
        self.calls.append((path, repo, params))
        headers = {"X-RateLimit-Remaining": str(self.remaining)}
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "nope"}, headers=headers)
        parts = path.strip("/").split("/")
        slug = f"{parts[1]}/{parts[2]}"
        if len(parts) == 5:
            pr = self.prs.get((slug, int(parts[4])))
            return httpx.Response(200 if pr else 404, json=pr or {"message": "Not Found"}, headers=headers)
        page = int((params or {}).get("page", 1))
        listing = [p for (s, _), p in sorted(self.prs.items(), key=lambda kv: -kv[0][1]) if s == slug]
        return httpx.Response(200, json=listing[(page - 1) * 100 : page * 100], headers=headers)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"merge sync must only read from the forge; it called client.{name}")


def make_sweep(recorder, delivery_repo, github, **settings) -> MergeSyncSweep:
    """A sweep that acts. The shipped default is `shadow` (see the mode test);
    tests about what the sweep RECORDS ask for `on` explicitly."""
    settings.setdefault("mode", "on")
    return MergeSyncSweep(
        client=github,
        recorder=recorder,
        delivery_repo=delivery_repo,
        settings=MergeSyncSettings(**settings),
        clock=lambda: NOW,
    )


def url(number: int, repo: str = REPO) -> str:
    return f"https://github.com/{repo}/pull/{number}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://github.com/acme/widgets/pull/7", ("acme/widgets", 7)),
        ("https://github.com/acme/widgets/pull/7/files", ("acme/widgets", 7)),
        ("https://github.com/acme/old/pull/418", ("acme/new", 418)),
        ("https://gitlab.com/acme/widgets/-/merge_requests/7", None),
        ("https://github.com/acme/widgets/issues/7", None),
        ("https://github.com/acme/widgets/pull/seven", None),
        ("", None),
    ],
)
def test_parse_github_pr_url(raw, expected, monkeypatch):
    monkeypatch.setattr(
        "embry0.storage.repo_renames.REPO_RENAMES",
        {"acme/old": "acme/new"},
    )
    assert parse_github_pr_url(raw) == expected


def test_a_bad_mode_is_refused_when_the_settings_are_built():
    with pytest.raises(ValueError, match="MERGE_SYNC_MODE"):
        MergeSyncSettings(mode="yes")


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_missed_merge_and_a_repo_without_a_webhook_are_both_corrected(seed, recorder, delivery_repo):
    """No webhook ever arrives here: the sweep alone makes the DB match GitHub."""
    failed = await seed.job(status="failed", pr_url=url(1))
    completed = await seed.job(status="completed", pr_url=url(2, "acme/no-webhook"), repo="acme/no-webhook")
    closed = await seed.job(status="completed", pr_url=url(3))
    still_open = await seed.job(status="completed", pr_url=url(4))
    github = FakeGitHub(
        [
            pr_object(1, merged=MERGED_AT),
            pr_object(2, repo="acme/no-webhook", merged=MERGED_AT),
            pr_object(3, closed=MERGED_AT),
            pr_object(4),
        ]
    )

    report = await make_sweep(recorder, delivery_repo, github).run_once()

    assert await seed.delivery(failed) == ("failed", "merged")
    assert await seed.delivery(completed) == ("pr_merged", "merged")
    assert await seed.delivery(closed) == ("pr_closed", "closed")
    assert await seed.delivery(still_open) == ("completed", "pr_open")
    assert {c["job_id"] for c in report["changes"]} == {failed, completed, closed}
    assert report["stopped"] is None


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_second_sweep_changes_nothing_and_asks_only_about_unsettled_prs(seed, recorder, delivery_repo):
    await seed.job(status="failed", pr_url=url(1))
    open_job = await seed.job(status="completed", pr_url=url(4))
    github = FakeGitHub([pr_object(1, merged=MERGED_AT), pr_object(4, updated=T0 + timedelta(hours=5))])
    sweep = make_sweep(recorder, delivery_repo, github)
    await sweep.run_once()
    before = (await seed.row(open_job))["updated_at"]
    github.calls.clear()

    report = await sweep.run_once()

    assert report["changes"] == []
    assert [c[0] for c in github.calls if c[0].count("/") == 5] == [f"/repos/{REPO}/pulls/4"]
    # an open PR somebody commented on does not rewrite the job row every sweep
    github.prs[(REPO, 4)] = pr_object(4, updated=T0 + timedelta(hours=9))
    await sweep.run_once()
    assert (await seed.row(open_job))["updated_at"] == before


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_the_sweep_finds_a_pr_a_human_opened_from_a_failed_jobs_branch(seed, recorder, delivery_repo):
    job = await seed.job(status="failed", task="Tasks frontend: to-do list row", created=NOW - timedelta(days=3))
    unrelated = pr_object(8, ref="feature/other", created=NOW - timedelta(days=2))
    human = pr_object(
        9,
        ref="embry0/unknown-tasks-frontend-to-do-list-row",
        created=NOW - timedelta(days=2),
        merged=NOW - timedelta(days=1),
    )
    github = FakeGitHub([unrelated, human])

    report = await make_sweep(recorder, delivery_repo, github).run_once()

    row = await seed.row(job)
    assert (row["status"], row["delivery_state"], row["pr_url"]) == ("failed", "merged", url(9))
    assert report["checked"] == 1  # only the PR whose head is a job's branch was recorded
    listing = [c for c in github.calls if c[0] == f"/repos/{REPO}/pulls"]
    assert listing[0][2]["state"] == "all" and listing[0][2]["sort"] == "updated"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_the_sweep_lists_prs_from_a_jobs_own_branch(seed, recorder, delivery_repo):
    """#478: the sweep only records PRs whose head is a branch a job may have pushed."""
    task = "Tasks frontend: to-do list row"
    job = await seed.job(status="failed", task=task, created=NOW - timedelta(days=3))
    own = f"embry0/unknown-tasks-frontend-to-do-list-row-{job.removeprefix('job-')[:8]}"
    human = pr_object(9, ref=own, created=NOW - timedelta(days=2), merged=NOW - timedelta(days=1))
    github = FakeGitHub([human])

    report = await make_sweep(recorder, delivery_repo, github).run_once()

    row = await seed.row(job)
    assert (row["delivery_state"], row["pr_url"]) == ("merged", url(9)), f"the PR from {own} was not recorded: {row}"
    assert report["checked"] == 1


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_old_unlinked_jobs_are_left_to_the_backfill(seed, recorder, delivery_repo):
    job = await seed.job(status="failed", task="Tasks frontend: to-do list row", created=NOW - timedelta(days=200))
    human = pr_object(
        9,
        ref="embry0/unknown-tasks-frontend-to-do-list-row",
        created=NOW - timedelta(days=199),
        merged=NOW - timedelta(days=198),
    )
    github = FakeGitHub([human])
    sweep = make_sweep(recorder, delivery_repo, github)

    await sweep.run_once()
    assert github.calls == [] and (await seed.delivery(job))[1] == "none"

    await sweep.run_once(full=True, dry_run=False)
    assert await seed.delivery(job) == ("failed", "merged")


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_sweep_is_bounded_and_resumes_where_it_stopped(seed, recorder, delivery_repo):
    jobs = [await seed.job(status="failed", pr_url=url(n), created=T0 + timedelta(minutes=n)) for n in range(1, 6)]
    # PRs 1-2 stay open, so a sweep that always restarted from the oldest would never reach 3-5.
    github = FakeGitHub([pr_object(n) for n in (1, 2)] + [pr_object(n, merged=MERGED_AT) for n in (3, 4, 5)])
    sweep = make_sweep(recorder, delivery_repo, github, max_requests_per_sweep=2, unlinked_lookback_days=0)

    first = await sweep.run_once()
    assert first["requests"] == 2 and first["changes"] == []
    for _ in range(3):
        await sweep.run_once()

    assert [(await seed.delivery(j))[1] for j in jobs] == ["pr_open", "pr_open", "merged", "merged", "merged"]
    assert all(len(c) for c in github.calls)


@pytest.mark.requires_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "remaining", "reason"), [(200, 10, "rate_limit_floor"), (403, 0, "http_403"), (429, 0, "http_429")]
)
async def test_the_sweep_stops_as_soon_as_github_says_it_is_running_low(
    seed, recorder, delivery_repo, status, remaining, reason
):
    for n in range(1, 4):
        await seed.job(status="failed", pr_url=url(n))
    github = FakeGitHub([pr_object(n, merged=MERGED_AT) for n in range(1, 4)], remaining=remaining, status=status)

    report = await make_sweep(recorder, delivery_repo, github).run_once()

    assert report["stopped"] == reason
    assert len(github.calls) == 1


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_shadow_mode_reports_and_writes_nothing_and_off_does_nothing(seed, recorder, delivery_repo):
    job = await seed.job(status="failed", pr_url=url(1))
    github = FakeGitHub([pr_object(1, merged=MERGED_AT)])

    shadow = await make_sweep(recorder, delivery_repo, github, mode="shadow").run_once()
    assert shadow["dry_run"] is True and [c["job_id"] for c in shadow["changes"]] == [job]
    assert await seed.delivery(job) == ("failed", "pr_open")

    off = make_sweep(recorder, delivery_repo, FakeGitHub([]), mode="off")
    assert off.enabled is False
    assert (await off.run_once())["status"] == "disabled"


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_the_check_lists_exactly_the_rows_where_the_db_disagrees_with_github(seed, recorder, delivery_repo):
    wrong = await seed.job(status="failed", pr_url=url(1))
    right = await seed.job(status="completed", pr_url=url(2))
    gone = await seed.job(status="completed", pr_url=url(3))
    gitlab = await seed.job(status="completed", pr_url="https://gitlab.com/acme/widgets/-/merge_requests/4")
    github = FakeGitHub([pr_object(1, merged=MERGED_AT), pr_object(2)])
    sweep = make_sweep(recorder, delivery_repo, github)

    check = await sweep.run_once(full=True, dry_run=True)

    assert [(c["job_id"], c["delivery_from"], c["delivery_to"]) for c in check["changes"]] == [
        (wrong, "pr_open", "merged")
    ]
    assert {(u["pr_url"], u["reason"]) for u in check["unverifiable"]} == {
        (url(3), "http_404"),
        ("https://gitlab.com/acme/widgets/-/merge_requests/4", "not_a_github_pr_url"),
    }
    assert await seed.delivery(wrong) == ("failed", "pr_open")  # a check never writes

    await sweep.run_once(full=True, dry_run=False)
    assert (await sweep.run_once(full=True, dry_run=True))["changes"] == []
    del right, gone, gitlab


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_the_backfill_rechecks_settled_rows_and_fills_their_forge_dates(
    seed, recorder, delivery_repo, db_with_migrations
):
    """Rows migration 73 seeded from `status` carry no forge time; and a row the
    old webhook marked from a closed PR that was later merged must be corrected."""
    seeded = await seed.job(status="completed", pr_url=url(1))
    await db_with_migrations.execute(
        "UPDATE jobs SET status = 'pr_closed', delivery_state = 'closed', delivery_state_at = NULL WHERE job_id = $1",
        seeded,
    )
    github = FakeGitHub([pr_object(1, merged=MERGED_AT)])

    await make_sweep(recorder, delivery_repo, github).run_once(full=True, dry_run=False)

    row = await seed.row(seeded)
    assert (row["status"], row["delivery_state"], row["delivery_state_at"]) == ("pr_merged", "merged", MERGED_AT)


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_job_that_completes_after_its_pr_merged_gets_its_status_aligned(
    seed, recorder, delivery_repo, db_with_migrations
):
    job = await seed.job(status="running", pr_url=url(1))
    github = FakeGitHub([pr_object(1, merged=MERGED_AT)])
    sweep = make_sweep(recorder, delivery_repo, github)
    await sweep.run_once()
    assert await seed.delivery(job) == ("running", "merged")

    await db_with_migrations.execute("UPDATE jobs SET status = 'completed' WHERE job_id = $1", job)
    report = await sweep.run_once()

    assert await seed.delivery(job) == ("pr_merged", "merged")
    assert [(c["matched_by"], c["status_to"]) for c in report["changes"]] == [("status", "pr_merged")]


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_one_failing_request_does_not_end_the_sweep(seed, recorder, delivery_repo):
    await seed.job(status="failed", pr_url=url(1), created=T0 - timedelta(hours=3))
    second = await seed.job(status="failed", pr_url=url(2))

    class Flaky(FakeGitHub):
        async def get(self, path, **kwargs):
            if path.endswith("/pulls/1"):
                raise httpx.ConnectError("boom")
            return await super().get(path, **kwargs)

    report = await make_sweep(recorder, delivery_repo, Flaky([pr_object(2, merged=MERGED_AT)])).run_once()

    assert report["errors"] == 1
    assert await seed.delivery(second) == ("failed", "merged")


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_repos_on_another_forge_are_not_listed(seed, recorder, delivery_repo):
    await seed.job(status="failed", task="Anything", created=NOW - timedelta(days=1))
    github = FakeGitHub([])

    async def never(_: str) -> bool:
        return False

    sweep = MergeSyncSweep(
        client=github,
        recorder=recorder,
        delivery_repo=delivery_repo,
        settings=MergeSyncSettings(),
        is_github=never,
        clock=lambda: NOW,
    )
    await sweep.run_once()
    assert github.calls == []


def test_settings_from_config_reads_every_knob():
    from embry0.config import Embry0Config
    from embry0.services.merge_sync.sweep import settings_from_config

    config = Embry0Config(
        _env_file=None,
        merge_sync_mode="on",
        merge_sync_interval_seconds=60,
        merge_sync_max_requests=7,
        merge_sync_max_writes=11,
        merge_sync_rate_limit_floor=9,
        merge_sync_list_pages=2,
        merge_sync_unlinked_lookback_days=3,
    )
    assert settings_from_config(config) == MergeSyncSettings("on", 60, 7, 11, 2, 3, 9)


def test_the_sweep_ships_in_shadow():
    """It writes delivery_state, job status, pr_url, closes embry0 issues and
    moves ledger cases with no human in the loop, so a human turns it on."""
    from embry0.config import Embry0Config
    from embry0.services.merge_sync.sweep import settings_from_config

    assert MergeSyncSettings().mode == "shadow"
    assert settings_from_config(Embry0Config(_env_file=None)).mode == "shadow"


def test_a_misconfigured_mode_idles_the_sweep_instead_of_stopping_startup():
    from embry0.config import Embry0Config
    from embry0.services.merge_sync.sweep import settings_from_config

    config = Embry0Config(_env_file=None, merge_sync_mode="enabled", merge_sync_interval_seconds=-5)
    settings = settings_from_config(config)
    assert (settings.mode, settings.interval_seconds) == ("off", 0)


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_transferred_repo_is_followed_and_recorded_under_the_stored_url(seed, recorder, delivery_repo):
    """GitHub answers 301 for a renamed or transferred repo, and the PR it then
    serves carries the NEW slug — which equals no stored row."""
    stored = url(33, "old-owner/widgets")
    job = await seed.job(status="completed", pr_url=stored, repo="old-owner/widgets")
    moved = pr_object(33, repo="new-owner/widgets", merged=MERGED_AT)

    class Transferred(FakeGitHub):
        async def get(self, path, **kwargs):
            if path.startswith("/repos/old-owner/"):
                self.calls.append((path, kwargs.get("repo"), kwargs.get("params")))
                target = "https://api.github.com/repositories/42" + path.removeprefix("/repos/old-owner/widgets")
                return httpx.Response(301, headers={"Location": target})
            if path.startswith("https://api.github.com/repositories/42"):
                path = "/repos/new-owner/widgets" + path.removeprefix("https://api.github.com/repositories/42")
            return await super().get(path, **kwargs)

    report = await make_sweep(recorder, delivery_repo, Transferred([moved])).run_once()

    assert await seed.delivery(job) == ("pr_merged", "merged")
    assert report["unverifiable"] == []


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_check_reports_each_pr_once_even_when_both_passes_see_it(seed, recorder, delivery_repo):
    owner = await seed.job(status="failed", pr_url=url(1), task="Fix the thing", created=NOW - timedelta(days=3))
    await seed.job(status="failed", task="Something unlinked", created=NOW - timedelta(days=3))
    follow_up = await seed.job(
        status="failed",
        task="follow-up",
        context={"ref": "embry0/unknown-fix-the-thing"},
        created=NOW - timedelta(days=1),
    )
    merged = pr_object(1, created=NOW - timedelta(days=2), merged=NOW - timedelta(hours=5))

    check = await make_sweep(recorder, delivery_repo, FakeGitHub([merged])).run_once(full=True, dry_run=True)

    assert sorted(c["job_id"] for c in check["changes"]) == sorted([owner, follow_up])
    assert check["checked"] == 1


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_a_sweep_stops_once_it_has_changed_its_budget_of_rows(seed, recorder, delivery_repo):
    """A request budget does not bound writes: one listing request can yield
    many facts, and one fact can touch several jobs."""
    jobs = [await seed.job(status="failed", pr_url=url(n), created=T0 + timedelta(minutes=n)) for n in range(1, 6)]
    github = FakeGitHub([pr_object(n, merged=MERGED_AT) for n in range(1, 6)])
    sweep = make_sweep(recorder, delivery_repo, github, max_writes_per_sweep=2, unlinked_lookback_days=0)

    first = await sweep.run_once()

    assert first["stopped"] == "write_budget"
    assert first["writes"] == 2
    settled = [(await seed.delivery(j))[1] for j in jobs]
    assert settled.count("merged") == 2 and settled.count("pr_open") == 3

    while (await sweep.run_once())["stopped"] == "write_budget":
        pass
    assert [(await seed.delivery(j))[1] for j in jobs] == ["merged"] * 5


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_an_operators_backfill_is_not_write_budgeted(seed, recorder, delivery_repo):
    jobs = [await seed.job(status="failed", pr_url=url(n)) for n in range(1, 6)]
    github = FakeGitHub([pr_object(n, merged=MERGED_AT) for n in range(1, 6)])
    sweep = make_sweep(recorder, delivery_repo, github, max_writes_per_sweep=1)

    report = await sweep.run_once(full=True, dry_run=False)

    assert report["stopped"] is None
    assert [(await seed.delivery(j))[1] for j in jobs] == ["merged"] * 5
