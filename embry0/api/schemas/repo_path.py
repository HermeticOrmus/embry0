"""One repo-path rule for every request schema (spec 3.5).

Two or more slash-separated segments, each starting alphanumeric — the rule
``POST /repos`` already enforced (#162) so a GitLab nested subgroup like
``acme/widgets/shop`` is expressible wherever a repo is named. GitHub
paths remain two segments; nothing here loosens them. The ``/repos``
sub-resource routes are ``{full_repo:path}`` catch-alls, so the server side
has always been able to address these; only the request schemas refused.

Anchored with ``\\Z``, not ``$``: ``$`` also matches before a trailing
newline, so ``"owner/name\\n"`` would pass and then flow into clone URLs and
sanitised container/volume names. ``embry0_mcp`` restates this rule verbatim
(it cannot import from ``embry0``), so the copied rule is the strict one.
"""

from __future__ import annotations

import re

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
REPO_PATH_PATTERN = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT})+\Z")

REPO_PATH_ERROR = "repo must be 'group/name' or 'group/subgroup/name'"


def validate_repo_path(v: str) -> str:
    """Return ``v`` unchanged, or raise ValueError naming the accepted shapes."""
    if not REPO_PATH_PATTERN.match(v):
        raise ValueError(REPO_PATH_ERROR)
    return v
