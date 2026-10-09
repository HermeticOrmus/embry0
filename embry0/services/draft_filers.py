"""The two draft filers the watcher can be given (#378).

``LinearDraftFiler`` wraps what the watcher did inline until now, unchanged in
behaviour. ``GitHubDraftFiler`` is new: filing a finding as a GitHub issue is
what lets a stack whose tracker is GitHub Issues — embry0's own — be watched.

Both are bound to one target and both file label-free: the trigger label is
the human's acceptance step (``trackers/filing.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from embry0.trackers.filing import Draft, DraftState

logger = structlog.get_logger(__name__)

#: Linear workflow-state types that end a draft, mapped to the shared
#: vocabulary. ``duplicate`` is a rejection, the same as GitHub's close
#: reason of that name: the human answered, so reconcile must free the slot
#: (#415). Anything else ("triage", "backlog", "started", …) is open.
_LINEAR_CLOSED = {"completed": "completed", "canceled": "canceled", "duplicate": "canceled"}

#: GitHub closes an issue with a reason. "not_planned" is a rejection — the
#: equivalent of a Linear cancel — while "completed" (or no reason at all,
#: which is what the API returns for older closures) means done.
_GITHUB_CLOSE_REASON: dict[str, DraftState] = {
    "not_planned": "canceled",
    "completed": "completed",
    "duplicate": "canceled",
}


class LinearDraftFiler:
    """Files into one Linear team (optionally one project)."""

    def __init__(self, linear_sync: Any, *, team_id: str, project_id: str | None = None) -> None:
        self._linear = linear_sync
        self._team_id = team_id
        self._project_id = project_id or None

    @property
    def tracker(self) -> str:
        return "linear"

    @property
    def target(self) -> str:
        return self._team_id

    async def file_draft(self, *, title: str, description: str) -> Draft | None:
        issue = await self._linear.create_issue(
            team_id=self._team_id, title=title, description=description, project_id=self._project_id
        )
        if not issue:
            return None
        return Draft(key=str(issue["identifier"]), title=title, url=str(issue.get("url") or ""))

    async def list_open_drafts(self, *, limit: int = 30) -> list[Draft]:
        issues = await self._linear.list_open_issues(self._team_id, project_id=self._project_id, limit=limit)
        return [
            Draft(
                key=str(i["identifier"]),
                title=str(i.get("title") or ""),
                url=str(i.get("url") or ""),
                label=str(i.get("state") or ""),
            )
            for i in issues
        ]

    async def draft_states(self, keys: Sequence[str]) -> dict[str, DraftState]:
        states = await self._linear.issue_state_types(self._team_id, list(keys))
        return {key: _LINEAR_CLOSED.get(states[key], "open") for key in states}  # type: ignore[misc]


class GitHubDraftFiler:
    """Files issues on one GitHub repo.

    GitHub's issue list includes pull requests — every PR is an issue with a
    ``pull_request`` key — so anything carrying that key is dropped. Left in,
    the watcher's adopt-scan and briefing would read the repo's PRs as open
    drafts.
    """

    def __init__(self, github_client: Any, *, repo: str) -> None:
        self._client = github_client
        self._repo = repo

    @property
    def tracker(self) -> str:
        return "github"

    @property
    def target(self) -> str:
        return self._repo

    async def file_draft(self, *, title: str, description: str) -> Draft | None:
        issue = await self._client.create_issue(self._repo, title=title, body=description)
        if not issue:
            return None
        logger.info("watcher_github_draft_filed", repo=self._repo, number=issue.get("number"))
        return Draft(key=str(issue["number"]), title=title, url=str(issue.get("html_url") or ""))

    async def list_open_drafts(self, *, limit: int = 30) -> list[Draft]:
        issues = await self._client.list_issues(self._repo, state="open", limit=limit)
        return [
            Draft(
                key=str(i["number"]),
                title=str(i.get("title") or ""),
                url=str(i.get("html_url") or ""),
                label=str(i.get("state") or "open"),
            )
            for i in issues
            if "pull_request" not in i
        ]

    async def draft_states(self, keys: Sequence[str]) -> dict[str, DraftState]:
        states: dict[str, DraftState] = {}
        for key in keys:
            try:
                number = int(str(key))
            except ValueError:
                # Not a GitHub key at all (a Linear id on a re-pointed stack).
                states[str(key)] = "missing"
                continue
            issue = await self._client.get_issue(self._repo, number)
            if issue is None:
                states[str(key)] = "missing"
            elif issue.get("state") == "open":
                states[str(key)] = "open"
            else:
                states[str(key)] = _GITHUB_CLOSE_REASON.get(str(issue.get("state_reason") or ""), "completed")
        return states


def build_filer(stack: Any, *, linear_sync: Any = None, github_token: str = "", credential_resolver: Any = None) -> Any:
    """The filer for a stack's tracker, or None when it cannot be built.

    None is not an error here: the tick records "no draft filer configured"
    and files nothing, which is what a stack pointed at an unconfigured
    tracker should do — rather than filing its findings somewhere else.
    """
    if stack.tracker == "linear":
        team_id = str((stack.tracker_target or {}).get("team_id") or "")
        if linear_sync is None or not team_id:
            return None
        return LinearDraftFiler(
            linear_sync, team_id=team_id, project_id=str((stack.tracker_target or {}).get("project_id") or "") or None
        )
    if stack.tracker == "github":
        from embry0.execution.github_client import GitHubClient

        # Findings about a stack are filed in that stack's own repo unless the
        # row says otherwise (a monorepo filing into its umbrella issue tracker).
        repo = str((stack.tracker_target or {}).get("repo") or stack.repo)
        return GitHubDraftFiler(
            GitHubClient(default_token=github_token, credential_resolver=credential_resolver), repo=repo
        )
    logger.warning("watcher_filer_unavailable", stack=stack.repo, tracker=stack.tracker)
    return None
