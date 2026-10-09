"""Operator dispatch: Linear ticket -> embry0 job.

The pure functions here (compose/build) are the reusable seed of a
Linear-trigger adapter; `main()` is the operator CLI on top. The fetch
drives :class:`embry0.services.linear_sync.LinearSyncService` — #163
deleted this module's own sync client and dataclass, so there is one
Linear GraphQL client, one query, and one issue shape
(:class:`embry0.trackers.TrackerIssue`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any
from urllib.parse import quote

import httpx

from embry0.trackers import TrackerIssue

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
DEFAULT_PROFILE = "dev-python"


def compose_task(issue: TrackerIssue) -> str:
    """Ticket -> the job's ``task`` text: [id] title, body, Linear URL."""
    parts = [f"[{issue.key}] {issue.title}"]
    if issue.body.strip():
        parts.append(issue.body.strip())
    parts.append(f"Linear: {issue.url}")
    return "\n\n".join(parts)


def compose_additional_context(identifier: str) -> str:
    """The embry0 addendum appended to the developer prompt.

    Rides ``JobCreateRequest.additional_context`` (already wired into the
    developer prompt); the cloned repo's CLAUDE.md is auto-loaded by the
    in-sandbox Claude CLI, so conventions only need a pointer, not a copy.

    #163: the old "add the `embry0` label via the GitHub API" instruction is
    gone — it contradicted the developer prompt (the sandbox has no direct
    GitHub API access; ``gh`` is unauthenticated by design) and the label is
    now applied orchestrator-side by ``ensure_pr_for_branch``.
    """
    return (
        f"This job was dispatched from Linear ticket {identifier} by embry0.\n"
        "\n"
        "Repository conventions: follow the repo's CLAUDE.md. Use Conventional "
        f"Commits and include ({identifier}) in every commit subject.\n"
        "\n"
        # RAV-1406: never name a branch — some repos default to master (and
        # in one, the local `main` is an unrelated orphan).
        "Pull request: target the repository's DEFAULT branch (check "
        "`git symbolic-ref --short refs/remotes/origin/HEAD`; never assume "
        f"`main`); reference {identifier} in the PR body "
        "and state that the PR was opened autonomously by embry0."
    )


def build_job_payload(issue: TrackerIssue, repo: str, profile: str) -> dict[str, str]:
    """The exact ``POST /api/v1/jobs`` body for an operator dispatch."""
    return {
        "repo": repo,
        "task": compose_task(issue),
        "sandbox_profile": profile,
        "additional_context": compose_additional_context(issue.key),
    }


def dispatch_job(payload: dict[str, str], base_url: str, api_key: str) -> dict[str, Any]:
    """POST the composed payload to embry0's job API."""
    resp = httpx.post(
        f"{base_url.rstrip('/')}/api/v1/jobs",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60.0,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dispatch-embry0",
        description="Dispatch a Linear ticket to embry0 as a coding job.",
    )
    parser.add_argument("issue_id", help="Linear issue identifier, e.g. ENG-123")
    # No baked-in target repo — set EMBRY0_DEFAULT_REPO or pass --repo explicitly.
    parser.add_argument(
        "--repo",
        default=os.environ.get("EMBRY0_DEFAULT_REPO", ""),
        help="Target repo as owner/name (default: $EMBRY0_DEFAULT_REPO)",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--dry-run", action="store_true", help="Print the composed payload; do not POST.")
    args = parser.parse_args(argv)

    if not args.repo:
        print("--repo is required (or set EMBRY0_DEFAULT_REPO)", file=sys.stderr)
        return 2

    linear_key = os.environ.get("LINEAR_API_KEY", "")
    if not linear_key:
        print("LINEAR_API_KEY is not set", file=sys.stderr)
        return 2

    from embry0.services.linear_sync import LinearSyncService

    issue = asyncio.run(LinearSyncService(linear_key, {}).fetch_trigger_issue(args.issue_id))
    payload = build_job_payload(issue, repo=args.repo, profile=args.profile)

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    base_url = os.environ.get("EMBRY0_URL", "http://localhost:8200")
    api_key = os.environ.get("EMBRY0_API_KEY") or os.environ.get("API_KEY", "")
    if not api_key:
        print("EMBRY0_API_KEY (or API_KEY) is not set", file=sys.stderr)
        return 2

    job = dispatch_job(payload, base_url=base_url, api_key=api_key)
    job_id = job.get("job_id") or job.get("id", "")
    print(f"job_id: {job_id}")
    print(f"console: {base_url.rstrip('/')}/jobs/{job_id}")
    # Board deep link (Console kanban, 2026-07-08 design): repo-filtered view
    # of the live board so the dispatching session can watch the whole batch.
    # safe="" so the repo's slash is percent-encoded into the query param.
    print(f"board: {base_url.rstrip('/')}/console?repo={quote(args.repo, safe='')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
