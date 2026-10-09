"""GitHub ``author_association`` trust gate, shared by every inbound path.

Two inbound comment paths are privilege escalations for anyone who can type
in a comment box: the tagged PR comment that dispatches a job
(:mod:`embry0.services.comment_dispatch`) and the ``/answer`` directive that
answers an agent question and unblocks a paused pipeline
(:mod:`embry0.api.v1.webhooks`). On a public repo "can comment" is
"everyone", so both must agree on who is trusted — hence one allowlist
parser and one check here instead of a copy per call site (#253).

The allowlist itself is operator-configurable via
``COMMENT_DISPATCH_AUTHOR_ROLES`` (:attr:`embry0.config.Embry0Config.comment_dispatch_author_roles`);
:data:`DEFAULT_AUTHOR_ROLES` is its default.
"""

from __future__ import annotations

from typing import Any

# GitHub's author_association values that mean "has write-ish standing in
# this repo". NONE / CONTRIBUTOR / FIRST_TIME_CONTRIBUTOR deliberately do not.
DEFAULT_AUTHOR_ROLES = "OWNER,MEMBER,COLLABORATOR"


def parse_author_roles(raw: str | None) -> set[str]:
    """Split a comma-separated allowlist into an upper-cased role set."""
    return {r.strip().upper() for r in (raw or "").split(",") if r.strip()}


def check_comment_author(comment: dict[str, Any], allowed: set[str]) -> tuple[bool, str]:
    """(trusted?, reason) for a GitHub comment object.

    Bots are rejected before the association is consulted: embry0's own
    comments carry ``OWNER``-ish associations on repos it owns, and a bot
    answering its own question is a loop, not an answer. Both of GitHub's
    bot markers are checked — ``user.type == "Bot"`` (GitHub Apps and the
    Actions token) and the ``[bot]`` login suffix — because neither covers
    the other on its own.
    """
    user = comment.get("user") or {}
    if str(user.get("type") or "").lower() == "bot":
        return False, "bot_author"
    login = user.get("login") or ""
    if login.endswith("[bot]"):
        return False, "bot_author"
    association = (comment.get("author_association") or "").upper()
    if association not in allowed:
        return False, f"author_association_{association or 'missing'}_not_allowed"
    return True, "ok"
