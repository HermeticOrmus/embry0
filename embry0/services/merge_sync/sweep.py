"""MergeSyncSweep — reconcile recorded delivery with what GitHub says (#279).

Webhooks are the fast path, not the record: a delivery can be missed, and a
repo may have no webhook at all (seen in production on 2026-09-20).
The sweep asks GitHub directly, with READ-ONLY calls, and hands every answer
to the same :class:`DeliveryRecorder` the webhook uses. It only records —
nothing is dispatched, commented or pushed — so it ships ``on``; ``shadow``
logs what it would record and ``off`` never starts it.

One engine, three uses:

* the periodic sweep — tracked PRs that are not settled (``pr_open``), then
  one page of recently updated PRs per repo that has recent jobs without a PR
  URL (a PR opened from a job's branch by someone else);
* the one-off backfill (``full=True``) — every tracked PR in every state and
  every page of those repos;
* the DB-equals-GitHub check (``full=True, dry_run=True``) — the changes a
  backfill WOULD make are exactly the rows where the DB disagrees.

Bounded per sweep by a request budget, and rate-limit aware: it stops as soon
as GitHub reports fewer than ``rate_limit_floor`` calls left or answers 403 /
429, and resumes where it stopped on the next interval.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import structlog

from embry0.services.merge_sync.facts import DeliveryState, fact_from_github
from embry0.services.merge_sync.recorder import DeliveryRecorder, aligned_status, branches_of
from embry0.storage.repo_renames import normalize_repo_slug, normalize_repo_url, repo_slug_variants
from embry0.storage.repositories.job_delivery import DeliveryWrite, JobDeliveryRepository

logger = structlog.get_logger(__name__)

MODES = ("off", "shadow", "on")
_GITHUB_HOSTS = ("github.com", "www.github.com")
_PAGE_SIZE = 100
# A full run pages until a repo is exhausted; this only stops a runaway loop.
_FULL_MAX_PAGES = 100
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
_FIRST_SWEEP_DELAY_SECONDS = 60


@dataclass(frozen=True)
class MergeSyncSettings:
    """Every knob of the sweep — see ``docs/configuration.md``."""

    mode: str = "shadow"
    interval_seconds: int = 900
    max_requests_per_sweep: int = 60
    max_writes_per_sweep: int = 200
    list_pages_per_repo: int = 1
    unlinked_lookback_days: int = 30
    rate_limit_floor: int = 500

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"MERGE_SYNC_MODE must be one of {MODES}, got {self.mode!r}")


def settings_from_config(config: Any) -> MergeSyncSettings:
    """Settings from ``Embry0Config``. Merge sync is optional: a bad value is
    logged CRITICAL and the sweep idles (``off``) — it never raises into
    startup. Negative numbers are read as zero."""
    mode = str(getattr(config, "merge_sync_mode", "shadow") or "").strip().lower()
    if mode not in MODES:
        logger.critical("merge_sync_mode_invalid", value=mode, allowed=list(MODES), effective="off")
        mode = "off"

    def number(name: str, default: int) -> int:
        try:
            return max(0, int(getattr(config, name, default)))
        except (TypeError, ValueError):
            return default

    return MergeSyncSettings(
        mode=mode,
        interval_seconds=number("merge_sync_interval_seconds", 900),
        max_requests_per_sweep=number("merge_sync_max_requests", 60),
        max_writes_per_sweep=number("merge_sync_max_writes", 200),
        list_pages_per_repo=number("merge_sync_list_pages", 1),
        unlinked_lookback_days=number("merge_sync_unlinked_lookback_days", 30),
        rate_limit_floor=number("merge_sync_rate_limit_floor", 500),
    )


def parse_github_pr_url(url: str) -> tuple[str, int] | None:
    """``("owner/repo", number)`` for a GitHub PR URL, else None (GitLab MRs,
    anything malformed). The slug comes back normalised for renames."""
    parsed = urlparse(normalize_repo_url(url))
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc.lower() not in _GITHUB_HOSTS or len(parts) < 4 or parts[2] != "pull" or not parts[3].isdigit():
        return None
    return f"{parts[0]}/{parts[1]}", int(parts[3])


class _Budget:
    """What this run has left: GitHub requests, job rows it may change, and
    whether GitHub told us to stop asking.

    The write budget is the blast-radius bound a sweep needs and a request
    budget cannot give: one listing request can produce many recorded facts,
    and one fact can touch several job rows. Reaching it ends the sweep
    cleanly; the next one carries on from there.
    """

    def __init__(self, limit: int | None, floor: int, writes: int | None = None) -> None:
        self._left = limit
        self._floor = floor
        self._writes_left = writes
        self.requests = 0
        self.writes = 0
        self.stopped: str | None = None

    def spend_writes(self, rows: int) -> None:
        self.writes += rows
        if self._writes_left is not None:
            self._writes_left -= rows
            if self._writes_left <= 0 and not self.stopped:
                self.stopped = "write_budget"

    @property
    def exhausted(self) -> bool:
        return self.stopped is not None

    def take(self) -> bool:
        if self.stopped:
            return False
        if self._left is not None:
            if self._left <= 0:
                self.stopped = "request_budget"
                return False
            self._left -= 1
        self.requests += 1
        return True

    def observe(self, response: Any) -> bool:
        """False when this response means "stop asking"."""
        if response.status_code in (403, 429):
            self.stopped = f"http_{response.status_code}"
            return False
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) < self._floor:
            self.stopped = "rate_limit_floor"
        return True


class MergeSyncSweep:
    """``client`` is a ``GitHubClient`` (per-owner token routing, retries).
    ``is_github`` answers whether a repo lives on GitHub — a GitLab repo's PRs
    are not this sweep's to ask about."""

    def __init__(
        self,
        *,
        client: Any,
        recorder: DeliveryRecorder,
        delivery_repo: JobDeliveryRepository,
        settings: MergeSyncSettings,
        is_github: Callable[[str], Awaitable[bool]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._recorder = recorder
        self._delivery = delivery_repo
        self._settings = settings
        self._is_github = is_github
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        # Where the tracked-PR pass resumes when one sweep's budget does not
        # cover every unsettled PR: the (first_created, pr_url) of the last one
        # looked at. A keyset, because rows that settle leave the listing and
        # an offset would then skip whatever shifted up into their place.
        # In memory: after a restart the pass starts from the oldest again, so
        # a backlog bigger than one sweep needs a few uninterrupted intervals
        # (or one `merge-sync backfill`) to be worked through.
        self._tracked_cursor: tuple[datetime, str] | None = None
        # PR URLs already recorded in the running pass: the listing pass must
        # not report (or, on a dry run, double-count) what the tracked pass did.
        self._seen: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self._settings.mode != "off" and self._settings.interval_seconds > 0

    async def start(self) -> None:
        """Interval loop. The first sweep runs a minute after startup, so a
        deploy corrects history without waiting a whole interval and startup
        itself never waits on GitHub. One bad sweep is one log line."""
        logger.info("merge_sync_started", **self._settings.__dict__)
        await asyncio.sleep(min(_FIRST_SWEEP_DELAY_SECONDS, self._settings.interval_seconds))
        while True:
            try:
                result = await self.run_once()
                if result.get("changes") or result.get("errors") or result.get("stopped"):
                    logger.info("merge_sync_sweep", **{k: v for k, v in result.items() if k != "changes"})
            except Exception:
                logger.error("merge_sync_sweep_failed", exc_info=True)
            await asyncio.sleep(self._settings.interval_seconds)

    async def run_once(self, *, full: bool = False, dry_run: bool | None = None) -> dict[str, Any]:
        """One pass. ``dry_run`` defaults to the mode (``shadow`` → True)."""
        if self._settings.mode == "off" and dry_run is None:
            return {"status": "disabled", "reason": "MERGE_SYNC_MODE is off"}
        dry = self._settings.mode != "on" if dry_run is None else dry_run
        source = "backfill" if full else "sweep"
        async with self._lock:
            # An operator's explicit `backfill` run is deliberate and
            # unbounded; the unattended loop is bounded on both axes.
            budget = _Budget(
                None if full else self._settings.max_requests_per_sweep,
                self._settings.rate_limit_floor,
                None if full else self._settings.max_writes_per_sweep,
            )
            report: dict[str, Any] = {
                "status": "ok",
                "mode": self._settings.mode,
                "full": full,
                "dry_run": dry,
                "checked": 0,
                "changes": [],
                "unverifiable": [],
                "errors": 0,
            }
            self._seen = set()
            report["changes"] += await self._align_statuses(dry_run=dry)
            budget.spend_writes(len(report["changes"]))
            await self._sweep_tracked(budget, report, full=full, dry_run=dry, source=source)
            await self._sweep_unlinked(budget, report, full=full, dry_run=dry, source=source)
            report["requests"] = budget.requests
            report["writes"] = budget.writes
            report["stopped"] = budget.stopped
            return report

    # ------------------------------------------------------------ passes

    async def _align_statuses(self, *, dry_run: bool) -> list[dict[str, Any]]:
        """Local, no forge call: a job that was still running when its PR was
        merged completes later, and its status then follows its delivery."""

        def decide(row: dict[str, Any]) -> DeliveryWrite | None:
            status = aligned_status(row["status"], row["delivery_state"])
            return DeliveryWrite(status=status) if status else None

        changes: list[dict[str, Any]] = []
        for job in await self._delivery.misaligned_statuses():
            if dry_run:
                write = decide(job)
                before = job
            else:
                applied = await self._delivery.apply(job["job_id"], decide)
                before, write = applied if applied else (job, None)
            if write and write.status:
                changes.append(
                    {
                        "pr_url": before["pr_url"],
                        "job_id": before["job_id"],
                        "matched_by": "status",
                        "delivery_from": before["delivery_state"],
                        "delivery_to": before["delivery_state"],
                        "status_from": before["status"],
                        "status_to": write.status,
                        "pr_url_linked": False,
                    }
                )
        return changes

    async def _sweep_tracked(
        self, budget: _Budget, report: dict[str, Any], *, full: bool, dry_run: bool, source: str
    ) -> None:
        states = [s.value for s in DeliveryState] if full else [DeliveryState.NONE.value, DeliveryState.PR_OPEN.value]
        limit = 100_000 if full else self._settings.max_requests_per_sweep
        cursor = None if full else self._tracked_cursor
        rows = await self._delivery.list_tracked(states=states, limit=limit, after=cursor)
        if not rows and cursor is not None:
            # Past the tail: start over, so PRs that are still open are
            # re-checked rather than left behind the cursor for ever.
            self._tracked_cursor = None
            rows = await self._delivery.list_tracked(states=states, limit=limit)
        for row in rows:
            if budget.exhausted:
                break
            here = (row["first_created"], row["pr_url"])
            target = parse_github_pr_url(row["pr_url"])
            if target is None:
                if not full:
                    self._tracked_cursor = here
                else:
                    report["unverifiable"].append({"pr_url": row["pr_url"], "reason": "not_a_github_pr_url"})
                continue
            slug, number = target
            pr = await self._get(
                budget, f"/repos/{slug}/pulls/{number}", repo=slug, report=report, subject=row["pr_url"]
            )
            if budget.exhausted and pr is None:
                break
            if not full:
                self._tracked_cursor = here
            if pr is None:
                continue
            # Recorded under the URL the job stores: after a repo transfer
            # GitHub answers with the new slug, which equals no stored row.
            await self._record(
                pr, slug, report, dry_run=dry_run, source=source, budget=budget, stored_url=row["pr_url"]
            )
        if not full and not budget.exhausted and len(rows) < limit:
            # The whole remaining listing fitted in this sweep: next one starts
            # from the top again.
            self._tracked_cursor = None

    async def _sweep_unlinked(
        self, budget: _Budget, report: dict[str, Any], *, full: bool, dry_run: bool, source: str
    ) -> None:
        now = self._clock()
        window = now - _EPOCH if full else timedelta(days=self._settings.unlinked_lookback_days)
        repos = dict.fromkeys(
            normalize_repo_slug(r) for r in await self._delivery.repos_with_unlinked_jobs(since=now - window)
        )
        pages = _FULL_MAX_PAGES if full else self._settings.list_pages_per_repo
        for slug in repos:
            if budget.exhausted:
                return
            if slug.count("/") != 1 or (self._is_github is not None and not await self._is_github(slug)):
                continue
            candidates = await self._delivery.find_unlinked_in_repo(
                repo_slug_variants(slug), created_before=now, window=window, limit=5000
            )
            branches = {branch for job in candidates for branch in branches_of(job)}
            for page in range(1, pages + 1):
                listing = await self._get(
                    budget,
                    f"/repos/{slug}/pulls",
                    repo=slug,
                    report=report,
                    subject=slug,
                    params={
                        "state": "all",
                        "sort": "updated",
                        "direction": "desc",
                        "per_page": _PAGE_SIZE,
                        "page": page,
                    },
                )
                if not isinstance(listing, list):
                    break
                for pr in listing:
                    # A page already fetched is still spent one PR at a time:
                    # the write budget stops the recording too, not only the
                    # next request.
                    if budget.exhausted:
                        return
                    if isinstance(pr, dict) and (pr.get("head") or {}).get("ref") in branches:
                        await self._record(pr, slug, report, dry_run=dry_run, source=source, budget=budget)
                if len(listing) < _PAGE_SIZE:
                    break

    # ----------------------------------------------------------- helpers

    async def _get(
        self,
        budget: _Budget,
        path: str,
        *,
        repo: str,
        report: dict[str, Any],
        subject: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """The JSON body of a 200, else None (and the reason in the report)."""
        if not budget.take():
            return None
        try:
            response = await self._client.get(path, repo=repo, params=params)
        except Exception as exc:
            report["errors"] += 1
            report["unverifiable"].append({"pr_url": subject, "reason": f"request_failed: {type(exc).__name__}"})
            logger.warning("merge_sync_request_failed", path=path, error=str(exc))
            return None
        if not budget.observe(response):
            report["unverifiable"].append({"pr_url": subject, "reason": budget.stopped})
            return None
        location = response.headers.get("Location")
        if response.status_code in (301, 302, 307, 308) and location and budget.take():
            # A renamed or transferred repo. One hop, GET only, same token.
            try:
                response = await self._client.get(location, repo=repo, params=params)
            except Exception as exc:
                report["errors"] += 1
                report["unverifiable"].append({"pr_url": subject, "reason": f"request_failed: {type(exc).__name__}"})
                return None
            if not budget.observe(response):
                report["unverifiable"].append({"pr_url": subject, "reason": budget.stopped})
                return None
        if response.status_code != 200:
            # 404: the PR or repo is gone, or the token cannot see it.
            report["unverifiable"].append({"pr_url": subject, "reason": f"http_{response.status_code}"})
            return None
        return response.json()

    async def _record(
        self,
        pr: dict[str, Any],
        slug: str,
        report: dict[str, Any],
        *,
        dry_run: bool,
        source: str,
        budget: _Budget,
        stored_url: str | None = None,
    ) -> None:
        fact = fact_from_github(pr, repo=slug)
        if fact is None:
            return
        if stored_url:
            fact = replace(fact, url=normalize_repo_url(stored_url))
        if fact.url in self._seen:
            return
        self._seen.add(fact.url)
        report["checked"] += 1
        try:
            result = await self._recorder.record(fact, source=source, dry_run=dry_run)
        except Exception:
            # One unreadable PR must not end the sweep for the rest.
            report["errors"] += 1
            logger.warning("merge_sync_record_failed", pr_url=fact.url, exc_info=True)
            return
        changed = [{"pr_url": fact.url, **c.as_dict()} for c in result.changes if c.material]
        report["changes"] += changed
        budget.spend_writes(len(changed))
