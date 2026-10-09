"""embry0 merge-sync — the one-off backfill and the DB-equals-GitHub check (#279).

    python -m embry0.cli merge-sync check      # read-only; exit 1 when the DB disagrees
    python -m embry0.cli merge-sync backfill   # record what GitHub says, for all history

Both run the sweep's engine over EVERY tracked PR (all states, every page of
every repo with unlinked jobs) against the database in ``DATABASE_URL`` and
GitHub with the orchestrator's own tokens, using only GET requests. ``check``
writes nothing; ``backfill`` is idempotent — a second run reports no changes.

Exit codes: 0 the DB equals GitHub · 1 it does not (``check``) · 3 everything
readable matched but some PRs could not be read at all (deleted or moved repo,
GitLab MR, no token access, rate limit). **3 is not a failure** — history
normally contains a few such PRs — so an operator reads the change list, not
the exit code. Each unreadable PR is printed with its reason.

The report is the command's output and stdout is only ever the report — the
sweep's own log lines are sent to stderr by :func:`logs_to_stderr`, so
``--json`` can be piped into a tool that has to parse it.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def logs_to_stderr() -> Iterator[None]:
    """Send structlog's output to stderr for the block, leaving stdout to the
    report, then put the previous configuration back.

    The orchestrator's ``configure_structlog`` prints to stdout, which is
    right for a container whose stdout IS its log and wrong for a command
    whose stdout is a document someone parses. structlog's configuration is
    process-global, so this is scoped rather than set once: restoring it is
    what keeps the change from reaching anything else in the process.
    """
    import structlog

    previous = structlog.get_config()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    try:
        yield
    finally:
        structlog.configure(**previous)


def _credential_resolver(config: Any, db: Any) -> Any:
    """The registry resolver (spec 2026-09-29 §3.1), or None for the env-only lookup.

    None when this process has no real ENVIRONMENT_SECRET_KEY: a dev-mode
    orchestrator then encrypts with an ephemeral key this process cannot know,
    and decrypting with the wrong key would mark every registry row
    ``decryption_failed``.
    """
    from embry0.services.credentials.resolver import CredentialResolver
    from embry0.storage.encryption import INSECURE_EXAMPLE_SECRET_KEY, FernetSecretsProvider
    from embry0.storage.repositories.credentials import CredentialsRepository

    key = config.environment_secret_key
    if not key or key == INSECURE_EXAMPLE_SECRET_KEY:
        import structlog

        structlog.get_logger(__name__).warning("merge_sync_registry_unavailable", reason="no_secret_key")
        return None
    secrets = FernetSecretsProvider(secret_key=key, old_secret_keys=config.environment_secret_keys_old_list)
    return CredentialResolver(CredentialsRepository(db), secrets, config)


async def run(action: str, *, config: Any = None, client: Any = None) -> dict[str, Any]:
    """Run ``check`` or ``backfill`` and return the sweep's report."""
    from embry0.config import Embry0Config
    from embry0.execution.github_client import GitHubClient
    from embry0.services.healing.service import HealingCaseService
    from embry0.services.merge_sync.recorder import DeliveryRecorder
    from embry0.services.merge_sync.sweep import MergeSyncSettings, MergeSyncSweep
    from embry0.storage.database import DatabasePool
    from embry0.storage.repo_renames import configure_repo_renames
    from embry0.storage.repositories.healing_cases import HealingCasesRepository
    from embry0.storage.repositories.issues import IssuesRepository
    from embry0.storage.repositories.job_delivery import JobDeliveryRepository
    from embry0.storage.repositories.repos import ReposRepository

    if action not in ("check", "backfill"):
        raise ValueError(f"unknown merge-sync action {action!r}")
    config = config or Embry0Config()
    # The sweep matches stored URLs under a renamed repo's old slug too (#527).
    configure_repo_renames(config.repo_renames)
    db = DatabasePool(config.database_url, min_size=1, max_size=2)
    await db.connect()
    try:
        delivery_repo = JobDeliveryRepository(db)
        recorder = DeliveryRecorder(delivery_repo, IssuesRepository(db), HealingCaseService(HealingCasesRepository(db)))
        repos = ReposRepository(db, default_forge=config.default_forge)

        async def is_github(repo: str) -> bool:
            try:
                return str(await repos.forge_for(repo)) == "github"
            except Exception:
                return False

        sweep = MergeSyncSweep(
            client=client
            or GitHubClient(default_token=config.github_token, credential_resolver=_credential_resolver(config, db)),
            recorder=recorder,
            delivery_repo=delivery_repo,
            # The mode gates the periodic loop, not an operator's explicit run.
            settings=MergeSyncSettings(mode="on", rate_limit_floor=config.merge_sync_rate_limit_floor),
            is_github=is_github,
        )
        return await sweep.run_once(full=True, dry_run=action == "check")
    finally:
        await db.close()


def exit_code(action: str, report: dict[str, Any]) -> int:
    if action == "check" and report["changes"]:
        return 1
    if report["unverifiable"] or report["errors"] or report["stopped"]:
        return 3
    return 0


def render(action: str, report: dict[str, Any], *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(report, indent=2, default=str)
    verb = "would change" if report["dry_run"] else "changed"
    lines = [f"merge-sync {action}: {report['checked']} PRs read in {report['requests']} GitHub requests"]
    for change in report["changes"]:
        parts = [f"delivery {change['delivery_from']} -> {change['delivery_to']}"]
        if change["status_from"] != change["status_to"]:
            parts.append(f"status {change['status_from']} -> {change['status_to']}")
        if change["pr_url_linked"]:
            parts.append(f"linked by {change['matched_by']}")
        lines.append(f"  {verb}: {change['job_id']}  {change['pr_url']}  " + "; ".join(parts))
    for item in report["unverifiable"]:
        lines.append(f"  not verified: {item['pr_url']} ({item['reason']})")
    if report["unverifiable"]:
        lines.append(
            f"  {len(report['unverifiable'])} PR(s) could not be read at all (exit 3). "
            "That is not a failure: a deleted or moved repo, or one this token cannot see, "
            "stays unverifiable. What matters is the change list above."
        )
    if report["stopped"]:
        lines.append(f"  stopped early: {report['stopped']} - run again later")
    if not report["changes"]:
        lines.append("  the database matches GitHub for every PR that could be read")
    return "\n".join(lines)
