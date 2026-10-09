"""Filing a draft ticket, as a capability of a tracker (#378).

``IssueTracker`` (base.py) covers inbound webhooks and comments. Filing was
never part of it: the watcher called ``LinearSyncService.create_issue``
directly, so "file a finding" meant "file it in Linear", and every step that
follows — adopt-before-refile, reconcile, the cap — spoke Linear's dialect.
A second watched stack whose tracker is GitHub Issues needs the same three
operations against a different forge.

One filer is bound to one target: a Linear team (and optional project), or a
GitHub repo. The watcher holds the filer for its stack and never names a
tracker again.

Draft states are a shared vocabulary, not each forge's:

``open``       the draft is still waiting for a human
``completed``  closed as done — possibly fixed outside embry0 (#282 decides)
``canceled``   closed as rejected — this finding will never be accepted
``missing``    the tracker has no such ticket (deleted, moved, unreadable)

The distinction matters: a cancel closes the healing case, while completed
and missing only free the cap slot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

DraftState = Literal["open", "completed", "canceled", "missing"]

#: What a draft's title is prefixed with, whatever the tracker. The watcher's
#: adopt-before-refile match is exact equality on this (#278).
DRAFT_TITLE_PREFIX = "[watcher] "


@dataclass(frozen=True)
class Draft:
    """A filed draft, as any tracker reports it.

    ``key`` is what the ledger stores as ``tracker_key`` and a human reads:
    "ENG-123" on Linear, "412" on GitHub.
    """

    key: str
    title: str = ""
    url: str = ""
    state: DraftState = "open"
    #: What the tracker calls this state ("In Progress", "open"). Shown to the
    #: analysis agent in the briefing; decisions read ``state``.
    label: str = ""


@runtime_checkable
class DraftFiler(Protocol):
    """Files and reads back the watcher's draft tickets on one target."""

    @property
    def tracker(self) -> str:
        """The DB ``tracker`` discriminator: "linear", "github", "gitlab"."""
        ...

    @property
    def target(self) -> str:
        """Human-readable target, for logs: a team id, or "owner/repo"."""
        ...

    async def file_draft(self, *, title: str, description: str) -> Draft | None:
        """Create the ticket, NEVER labelled — applying the trigger label is
        the human acceptance step, and keeping every filer label-free makes
        the watcher's human gate structural rather than behavioural.

        Returns None when the tracker refused; raising is also allowed, and
        the watcher treats both as "not filed".
        """
        ...

    async def list_open_drafts(self, *, limit: int = 30) -> list[Draft]:
        """Open tickets on this target, newest activity first. Raises on
        failure so the caller can fail open deliberately."""
        ...

    async def draft_states(self, keys: Sequence[str]) -> dict[str, DraftState]:
        """State per key. A key the tracker does not know is ``missing``;
        keys absent from the result are treated the same way."""
        ...
