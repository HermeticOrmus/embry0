"""The two draft filers (#378): same three operations, two forges.

Linear's filer must behave exactly as the watcher's inline calls did; GitHub's
is new, and the states it reports are what reconciliation acts on — a wrong
mapping either frees a live draft's cap slot or closes a case nobody cancelled.
"""

from __future__ import annotations

from typing import Any

import pytest

from embry0.services.draft_filers import GitHubDraftFiler, LinearDraftFiler
from embry0.trackers.filing import Draft, DraftFiler


class FakeLinear:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.issue: dict[str, Any] | None = {"id": "uuid-1", "identifier": "ENG-7", "url": "https://l/ENG-7"}
        self.open_issues: list[dict[str, Any]] = []
        self.states: dict[str, str] = {}

    async def create_issue(self, **kwargs: Any) -> dict[str, Any] | None:
        self.created.append(kwargs)
        return self.issue

    async def list_open_issues(self, team_id: str, project_id: str | None = None, limit: int = 30) -> list[Any]:
        self.listed = {"team_id": team_id, "project_id": project_id, "limit": limit}
        return self.open_issues

    async def issue_state_types(self, team_id: str, identifiers: list[str]) -> dict[str, str]:
        return {k: v for k, v in self.states.items() if k in identifiers}


class FakeGitHubClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.issue: dict[str, Any] | None = {"number": 412, "html_url": "https://gh/412", "title": "t"}
        self.issues: list[dict[str, Any]] = []
        self.by_number: dict[int, dict[str, Any] | None] = {}

    async def create_issue(self, repo: str, *, title: str, body: str) -> dict[str, Any] | None:
        self.created.append({"repo": repo, "title": title, "body": body})
        return self.issue

    async def list_issues(self, repo: str, *, state: str = "open", limit: int = 30) -> list[dict[str, Any]]:
        self.listed = {"repo": repo, "state": state, "limit": limit}
        return self.issues

    async def get_issue(self, repo: str, number: int) -> dict[str, Any] | None:
        return self.by_number.get(number)


def test_both_filers_satisfy_the_protocol() -> None:
    assert isinstance(LinearDraftFiler(FakeLinear(), team_id="team"), DraftFiler)
    assert isinstance(GitHubDraftFiler(FakeGitHubClient(), repo="o/r"), DraftFiler)


# -- Linear: unchanged behaviour ------------------------------------------------


async def test_linear_files_into_its_team_and_project_without_labels() -> None:
    linear = FakeLinear()
    filer = LinearDraftFiler(linear, team_id="team-1", project_id="proj-1")
    draft = await filer.file_draft(title="[watcher] boom", description="body")

    assert (filer.tracker, filer.target) == ("linear", "team-1")
    assert draft == Draft(key="ENG-7", title="[watcher] boom", url="https://l/ENG-7")
    assert linear.created == [
        {"team_id": "team-1", "title": "[watcher] boom", "description": "body", "project_id": "proj-1"}
    ]
    assert "labels" not in linear.created[0], "the trigger label is the human's acceptance step"


async def test_linear_refusal_is_not_a_draft() -> None:
    linear = FakeLinear()
    linear.issue = None
    assert await LinearDraftFiler(linear, team_id="t").file_draft(title="x", description="y") is None


async def test_linear_state_types_map_to_the_shared_vocabulary() -> None:
    linear = FakeLinear()
    linear.states = {
        "ENG-1": "completed",
        "ENG-2": "canceled",
        "ENG-3": "started",
        "ENG-4": "triage",
        "ENG-5": "duplicate",
    }
    states = await LinearDraftFiler(linear, team_id="t").draft_states(
        ["ENG-1", "ENG-2", "ENG-3", "ENG-4", "ENG-5", "ENG-9"]
    )

    assert states["ENG-5"] == "canceled", (
        f"a Linear ticket marked duplicate is a human rejection and must free the slot (#415); got {states['ENG-5']!r}"
    )
    assert states == {
        "ENG-1": "completed",
        "ENG-2": "canceled",
        "ENG-3": "open",
        "ENG-4": "open",
        "ENG-5": "canceled",
    }
    assert "ENG-9" not in states, "a key the tracker does not know is absent (the caller reads that as missing)"


# -- GitHub: new ----------------------------------------------------------------


async def test_github_files_an_unlabelled_issue() -> None:
    client = FakeGitHubClient()
    filer = GitHubDraftFiler(client, repo="example-org/embry0")
    draft = await filer.file_draft(title="[watcher] boom", description="body")

    assert (filer.tracker, filer.target) == ("github", "example-org/embry0")
    assert draft == Draft(key="412", title="[watcher] boom", url="https://gh/412")
    assert client.created == [{"repo": "example-org/embry0", "title": "[watcher] boom", "body": "body"}]


async def test_github_listing_drops_pull_requests() -> None:
    """Every PR is an issue; left in, the adopt-scan and briefing would read
    the repo's PRs as open drafts."""
    client = FakeGitHubClient()
    client.issues = [
        {"number": 1, "title": "a real issue", "html_url": "https://gh/1"},
        {"number": 2, "title": "a PR", "html_url": "https://gh/2", "pull_request": {"url": "..."}},
    ]
    drafts = await GitHubDraftFiler(client, repo="o/r").list_open_drafts(limit=5)
    assert [d.key for d in drafts] == ["1"]


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        ({"state": "open"}, "open"),
        ({"state": "closed", "state_reason": "completed"}, "completed"),
        ({"state": "closed", "state_reason": None}, "completed"),
        ({"state": "closed", "state_reason": "not_planned"}, "canceled"),
        ({"state": "closed", "state_reason": "duplicate"}, "canceled"),
        (None, "missing"),
    ],
)
async def test_github_close_reasons_map_to_the_shared_vocabulary(issue: Any, expected: str) -> None:
    """not_planned is a rejection (closes the case); completed only frees the
    slot, because #282 decides whether it was fixed elsewhere."""
    client = FakeGitHubClient()
    client.by_number = {42: issue}
    assert await GitHubDraftFiler(client, repo="o/r").draft_states(["42"]) == {"42": expected}


async def test_a_key_that_is_not_a_github_number_is_missing() -> None:
    """A stack re-pointed from Linear still holds "RAV-1" keys; asking GitHub
    for them must free the slot, not raise."""
    assert await GitHubDraftFiler(FakeGitHubClient(), repo="o/r").draft_states(["ENG-1"]) == {"ENG-1": "missing"}


# -- the watcher files through whichever filer its stack has ---------------------


async def test_the_watcher_files_a_github_issue_and_records_its_tracker(monkeypatch: Any) -> None:
    """The point of the protocol: the same tick, filing into GitHub, records
    tracker "github" and a bare issue number as the key."""
    from tests.unit.services.test_watcher import _service

    client = FakeGitHubClient()
    client.issue = {"number": 412, "html_url": "https://github.com/example-org/embry0/issues/412"}
    job = {
        "job_id": "job-w1",
        "status": "completed",
        "result_summary": '{"title": "Orchestrator 500s", "body": "## Evidence\\nlines", "severity": "high"}',
    }
    svc = _service(lines=["[c] e1", "[c] e2", "[c] e3"], job_result=job, linear_issue={"identifier": "unused"})
    svc._filer = GitHubDraftFiler(client, repo="example-org/embry0")

    out = await svc.run_once()

    assert out["action"] == "proposed"
    assert (out["tracker"], out["tracker_key"]) == ("github", "412")
    assert out["tracker_url"] == "https://github.com/example-org/embry0/issues/412"
    assert client.created[0]["title"] == "[watcher] Orchestrator 500s"
    assert "labels" not in client.created[0], "filing must not dispatch the pipeline"
