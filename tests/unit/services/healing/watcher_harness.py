"""A WatcherService on real repositories with a scripted analysis agent and a
fake Linear — for the ledger's end-to-end tests. Nothing here leaves the process."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from embry0.services.draft_filers import LinearDraftFiler
from embry0.services.healing.recorder import CaseRecorder
from embry0.services.healing.service import HealingCaseService
from embry0.services.watcher import WatcherService
from embry0.storage.database import DatabasePool
from embry0.storage.repositories.healing_cases import HealingCasesRepository
from embry0.storage.repositories.log_signatures import LogSignaturesRepository
from embry0.storage.repositories.watcher_runs import WatcherRunsRepository


def _as_entries(lines, *, base=None):
    """#289: the tick reads structured entries. Stamps are spaced a second apart
    so a cluster has a non-zero span, and fixed so it is identical every run."""
    from datetime import UTC, datetime, timedelta

    from embry0.services.log_signatures import LogEntry

    start = base or datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    return [
        LogEntry(line=text, timestamp=start + timedelta(seconds=i), service="svc") for i, text in enumerate(lines or [])
    ]


LINES = ["[backend] boom 1", "[backend] boom 2", "[backend] boom 3"]


def proposal(title: str, severity: str = "medium") -> str:
    return f"TITLE: {title}\nSEVERITY: {severity}\nRATIONALE: recurring\nBODY:\n## Evidence\n{title} x40"


class ScriptedJobs:
    """Analysis jobs that finish at once with the next scripted deliverable."""

    def __init__(self, deliverables: list[str]) -> None:
        self.deliverables = list(deliverables)
        self.rows: dict[str, dict[str, Any]] = {}

    async def create(self, **_: Any) -> str:
        job_id = f"job-analysis-{len(self.rows) + 1}"
        self.rows[job_id] = {"job_id": job_id, "status": "completed", "result_summary": self.deliverables.pop(0)}
        return job_id

    async def get(self, job_id: str) -> dict[str, Any] | None:
        return self.rows.get(job_id)


class FakeLinear:
    def __init__(self, first_number: int = 1) -> None:
        self.created: list[dict[str, Any]] = []
        self.states: dict[str, str] = {}
        self._next = first_number

    async def create_issue(self, **kwargs: Any) -> dict[str, Any]:
        identifier = f"ENG-{self._next}"
        self._next += 1
        self.created.append({"identifier": identifier, **kwargs})
        self.states[identifier] = "backlog"
        return {"identifier": identifier, "url": f"https://linear.test/{identifier}"}

    async def list_open_issues(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return [
            {
                "identifier": i["identifier"],
                "state": self.states[i["identifier"]],
                "title": i["title"],
                "url": f"https://linear.test/{i['identifier']}",
            }
            for i in self.created
            if self.states.get(i["identifier"]) not in ("completed", "canceled")
        ]

    async def issue_state_types(self, _team: str, identifiers: list[str]) -> dict[str, str]:
        return {i: self.states[i] for i in identifiers if i in self.states}


def build_watcher(
    db: DatabasePool,
    *,
    repo: str,
    deliverables: list[str],
    max_open: int = 2,
    first_ticket: int = 1,
    linear: FakeLinear | None = None,
    distinct_windows: bool = False,
    **config: Any,
) -> tuple[WatcherService, FakeLinear, ScriptedJobs]:
    """A watcher on real repositories. Pass ``linear`` to model a restart:
    the process is new, the tracker is the same one it was talking to.

    ``distinct_windows`` gives every tick its own log window, so consecutive
    ticks carry different signatures and therefore model different FAULTS.
    Tests about the cap, deferral and adoption need it since #290: with one
    fixed window every tick sees the same signature, so the second tick is
    correctly a `skipped_duplicate` rather than a second finding. (In
    production #291 means an unchanged window is not re-analysed at all, so
    the repeated-identical-window fixture no longer describes anything real.)
    Leave it False to model exactly that: a genuine re-detection of one
    fault, which is what the dedupe tests are about."""
    cases_repo = HealingCasesRepository(db)
    linear, jobs = linear or FakeLinear(first_ticket), ScriptedJobs(deliverables)
    settings = {
        "watcher_interval_seconds": 3600,
        "watcher_loki_url": "http://loki.invalid",
        "watcher_logql": '{compose_project="x"}',
        "watcher_min_log_lines": 3,
        "watcher_max_log_lines": 50,
        "watcher_max_open_proposals": max_open,
        "watcher_max_deferred_findings": 3,
        "watcher_max_full_cap_analyses_per_day": 99,
        "watcher_proposal_window_days": 7,
        "watcher_job_timeout_seconds": 60,
        "watcher_linear_team_id": "team-1",
        "watcher_linear_project_id": "",
        "watcher_repo": repo,
        **config,
    }
    svc = WatcherService(
        config=SimpleNamespace(**settings),
        jobs_repo=jobs,
        watcher_runs_repo=WatcherRunsRepository(db),
        integration_repo=MagicMock(get_telegram_credentials=AsyncMock(return_value=("", ""))),
        executor=MagicMock(),
        # #378: the watcher files through a tracker filer now. The fake Linear
        # service is wrapped in the real LinearDraftFiler, so these DB-backed
        # tests exercise the production filing path, not a stand-in for it.
        filer=LinearDraftFiler(linear, team_id=settings["watcher_linear_team_id"]),
        cases_repo=cases_repo,
        case_recorder=CaseRecorder(HealingCaseService(cases_repo)),
        # #369/#290: the durable signature memory the production wiring passes.
        # Without it every tick reads as "nothing has ever been seen" and the
        # signature dedupe quietly degrades to the old title hash.
        signatures_repo=LogSignaturesRepository(db),
    )
    if distinct_windows:
        # The window follows the FAULT, which is what reality does: the same
        # fault logs the same lines, a different one logs different lines. The
        # tag comes from the title of the proposal this tick is about to be
        # served, so two ticks scripted with the same finding really do see one
        # signature (and dedupe), while different findings do not.
        #
        # Distinct WORDS, not distinct numbers: normalize_line() masks digits
        # by design, so "fault 1" and "fault 2" would collapse to one template
        # and the windows would not be distinct at all after clustering.
        def _tag() -> str:
            nxt = jobs.deliverables[0] if jobs.deliverables else "quiet"
            for line in nxt.splitlines():
                if line.startswith("TITLE:"):
                    return "".join(ch for ch in line[6:] if ch.isalpha()).lower() or "untitled"
            return "quiet"

        async def _window(*_a: Any, **_k: Any):
            tag = _tag()
            return _as_entries([f"[backend] Zed{tag}Exception: the {tag} subsystem refused" for _ in range(3)])

        svc._fetch_log_entries = _window  # type: ignore[method-assign]
    else:
        svc._fetch_log_entries = AsyncMock(return_value=_as_entries(LINES))  # type: ignore[method-assign]
    svc._fetch_recent_commits = AsyncMock(return_value=[])  # type: ignore[method-assign]
    svc._fetch_deploy_markers = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return svc, linear, jobs
