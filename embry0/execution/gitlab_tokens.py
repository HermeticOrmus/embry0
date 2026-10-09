"""Per-namespace GitLab token resolution (#164).

Mirrors :mod:`embry0.execution.github_tokens`: the orchestrator injects one
credential per sandbox, and a repo under a namespace the default token does
not cover picks up ``GITLAB_TOKEN__<NAMESPACE>`` from the orchestrator env.

The namespace is the **top-level group**, not the full path. GitLab nests
arbitrarily deep — ``acme/widgets/shop`` — but access tokens are issued
at the group or instance level, and the top-level group is the unit an
operator actually holds a token for. So that repo resolves
``GITLAB_TOKEN__ACME``, not ``GITLAB_TOKEN__ACME_PRODUCTS``.
"""

from __future__ import annotations

import os


def namespace_of(repo: str | None) -> str | None:
    """Top-level group of a GitLab project path, or None."""
    if not repo or "/" not in repo:
        return None
    return repo.split("/", 1)[0] or None


def resolve_gitlab_token(namespace: str | None, default: str) -> str:
    if not namespace:
        return default
    key = "GITLAB_TOKEN__" + namespace.upper().replace("-", "_").replace(".", "_")
    return os.environ.get(key) or default


def resolve_for_repo(repo: str | None, default: str) -> str:
    """Token for a project's top-level group; ``default`` when unset/slashless."""
    return resolve_gitlab_token(namespace_of(repo), default)
