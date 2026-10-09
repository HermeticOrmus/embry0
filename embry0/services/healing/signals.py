"""Non-watcher detection sources, translated into healing cases (#296, SH-P3.8).

Detection used to be one regex over one stack's logs. A deployment's
Prometheus, Alertmanager, Loki and cadvisor could page a chat, but none of them
could open a case, so a crash-looping container or a red CI run on `main` could
reach nobody — a page in a chat nobody reads is lost.

This module is the pure half: turn one Alertmanager alert, or one GitHub
`workflow_run`, into a :class:`Signal` — the fields ``HealingCaseService`` needs
and nothing else. No I/O, no database, so the attribution rules below are
testable against real payloads.

Two decisions worth stating:

* **A signal without a repo is dropped, loudly.** ``healing_cases.repo`` is NOT
  NULL, and guessing would file a case against the wrong repo, which is worse
  than not filing one. :func:`resolve_repo` tries the explicit label, then the
  configured map, then the single watched repo, and gives up.
* **The fingerprint is the source's own identity**, not a hash of the text.
  Alertmanager already computes a stable fingerprint per label set and resends
  it every ``repeat_interval``; a workflow run is identified by workflow and
  branch. Both are stable across rewordings for the same reason #290's
  signature id is.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from embry0.services.healing.lifecycle import Origin

logger = structlog.get_logger(__name__)

#: Label keys consulted, in order, when an alert carries no explicit `repo`.
#: Ordered most- to least-specific: a container name names one deployment, a
#: job names a scrape target that may cover several.
REPO_LABEL_CANDIDATES: tuple[str, ...] = ("repo", "service", "container_name", "name", "job")

#: Alertmanager severities mapped onto the ledger's vocabulary. Anything else
#: is passed through untouched — `severity` is free text on a case, and
#: inventing a mapping for an unknown value would lose information.
_SEVERITY_ALIASES = {"page": "critical", "warn": "warning", "info": "low", "none": "low"}

#: Alerts that are about embry0 itself. They still become cases — a loop that
#: cannot see its own failures is the thing this plan exists to fix — but they
#: are marked so nothing tries to send embry0 to fix embry0 automatically.
SELF_PREFIX = "Embry0"


@dataclass(frozen=True)
class Signal:
    """One detection, ready for the ledger."""

    repo: str
    origin: Origin
    title: str
    fingerprint: str
    severity: str | None = None
    signature: str | None = None
    note: str | None = None
    at: datetime | None = None
    resolved: bool = False
    #: True when this signal is about embry0's own deployment.
    about_self: bool = False
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def event_key(self) -> str:
        """Idempotency key for a redelivery of this exact signal.

        Alertmanager resends a firing group every ``repeat_interval`` (4h
        here), and a webhook may be retried on top of that. Keyed on the
        fingerprint, the start time and whether it is the firing or the
        resolved edge, so a retry is dropped while a genuine re-fire after a
        resolve is not.
        """
        edge = "resolved" if self.resolved else "firing"
        stamp = self.at.isoformat() if self.at else "-"
        return f"alert:{self.fingerprint}:{stamp}:{edge}"


def _parse_time(raw: Any) -> datetime | None:
    """A source timestamp, or None. Never a wall clock passed off as theirs.

    Alertmanager sends RFC3339, and uses the zero time (`0001-01-01T00:00:00Z`)
    for an `endsAt` that has not happened. A zero time is not a fact about when
    anything occurred, so it reads as absent.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return None if parsed.year <= 1 else parsed


def resolve_repo(labels: dict[str, str], *, repo_map: dict[str, str], default_repo: str | None) -> str | None:
    """Which repo this signal belongs to, or None.

    ``labels["repo"]`` is authoritative — an alert rule can name its repo and
    that ends the question. Otherwise each candidate label's *value* is looked
    up in the configured map. Only then does the single watched repo apply,
    and only when one is configured: on a multi-stack deployment (#297) that
    fallback is exactly the wrong guess, which is why it is last and why a miss
    returns None rather than something plausible.
    """
    explicit = (labels.get("repo") or "").strip()
    if explicit:
        return explicit
    for key in REPO_LABEL_CANDIDATES:
        value = (labels.get(key) or "").strip()
        if value and value in repo_map:
            return repo_map[value]
    return (default_repo or "").strip() or None


def parse_repo_map(raw: str) -> dict[str, str]:
    """Parse ALERTMANAGER_REPO_MAP. A broken value maps nothing, loudly.

    Deliberately not an exception: a typo in one env var must not stop the
    orchestrator, and a receiver that maps nothing answers "ignored, no repo
    mapping" with the reason in the log rather than 500-ing at Alertmanager.
    """
    if not (raw or "").strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.error("alertmanager_repo_map_unparseable", raw=raw[:200])
        return {}
    if not isinstance(parsed, dict):
        logger.error("alertmanager_repo_map_not_an_object", kind=type(parsed).__name__)
        return {}
    return {str(k): str(v) for k, v in parsed.items() if str(v).strip()}


def signal_from_alert(
    alert: dict[str, Any],
    *,
    repo_map: dict[str, str],
    default_repo: str | None,
) -> Signal | None:
    """One entry of an Alertmanager v4 webhook payload -> a Signal.

    Returns None when the alert has no alertname (not an alert) or no repo
    can be resolved — both are logged by the caller with the reason.
    """
    labels = {str(k): str(v) for k, v in (alert.get("labels") or {}).items()}
    annotations = {str(k): str(v) for k, v in (alert.get("annotations") or {}).items()}
    alertname = (labels.get("alertname") or "").strip()
    if not alertname:
        return None

    repo = resolve_repo(labels, repo_map=repo_map, default_repo=default_repo)
    if repo is None:
        return None

    summary = (annotations.get("summary") or "").strip()
    title = f"[alert] {alertname}" + (f": {summary}" if summary else "")

    severity = (labels.get("severity") or "").strip().lower() or None
    severity = _SEVERITY_ALIASES.get(severity, severity) if severity else None

    # Alertmanager's own fingerprint is stable per label set. Fall back to one
    # derived from the labels when a sender omits it, so the dedupe key never
    # silently becomes "every delivery is new".
    fingerprint = (alert.get("fingerprint") or "").strip()
    if not fingerprint:
        material = json.dumps(labels, sort_keys=True)
        fingerprint = hashlib.sha256(material.encode()).hexdigest()[:16]

    resolved = (alert.get("status") or "").strip().lower() == "resolved"
    generator = str(alert.get("generatorURL") or "").strip()
    note_parts = [
        annotations.get("description", "").strip(),
        f"generatorURL: {generator}" if generator else "",
    ]
    return Signal(
        repo=repo,
        origin=Origin.ALERT,
        title=title,
        fingerprint=fingerprint,
        severity=severity,
        # The alert's own expression is the closest thing it has to a
        # re-runnable signature. It is NOT LogQL, so #312 must not execute it;
        # it is stored for a human reading the case.
        signature=(labels.get("__expr__") or annotations.get("expr") or "").strip() or None,
        note="\n".join(p for p in note_parts if p) or None,
        at=_parse_time(alert.get("endsAt") if resolved else alert.get("startsAt")),
        resolved=resolved,
        about_self=alertname.startswith(SELF_PREFIX),
        labels=labels,
    )


def signals_from_alertmanager(
    payload: dict[str, Any],
    *,
    repo_map: dict[str, str],
    default_repo: str | None,
) -> tuple[list[Signal], list[str]]:
    """The whole webhook payload -> signals, plus a reason per alert skipped."""
    signals: list[Signal] = []
    skipped: list[str] = []
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        return [], ["payload carried no alerts array"]
    for alert in alerts:
        if not isinstance(alert, dict):
            skipped.append("alert entry was not an object")
            continue
        signal = signal_from_alert(alert, repo_map=repo_map, default_repo=default_repo)
        if signal is None:
            name = str((alert.get("labels") or {}).get("alertname", "?"))
            skipped.append(f"{name}: no alertname or no repo mapping")
            continue
        signals.append(signal)
    return signals, skipped


#: Conclusions of a completed workflow run that mean a broken `main`.
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})


def signal_from_workflow_run(payload: dict[str, Any], *, default_branch_only: bool = True) -> Signal | None:
    """A GitHub `workflow_run` event -> a Signal, or None if it is not a
    failure on the repo's default branch.

    Only the default branch: a red run on a feature branch is the pipeline
    working, and #305 already reacts to those on embry0's own PRs. A red run on
    `main` is a broken deployment target, which is a case.
    """
    run = payload.get("workflow_run") or {}
    repo = str((payload.get("repository") or {}).get("full_name") or "").strip()
    if not repo or not isinstance(run, dict):
        return None
    if str(run.get("status") or "").lower() != "completed":
        return None
    conclusion = str(run.get("conclusion") or "").lower()
    if conclusion not in FAILED_CONCLUSIONS:
        return None

    branch = str(run.get("head_branch") or "")
    default_branch = str((payload.get("repository") or {}).get("default_branch") or "main")
    if default_branch_only and branch != default_branch:
        return None

    name = str(run.get("name") or "workflow")
    # Keyed on repo + workflow + branch, NOT on the run id: every failing run
    # of the same workflow on the same branch is the same broken build, and
    # keying on the run would open a case per attempt.
    fingerprint = hashlib.sha256(f"ci:{repo}:{name}:{branch}".encode()).hexdigest()[:16]
    return Signal(
        repo=repo,
        origin=Origin.CI,
        title=f"[ci] {name} failing on {branch}",
        fingerprint=fingerprint,
        severity="high",
        note=f"conclusion={conclusion} run={run.get('html_url') or run.get('id')}",
        at=_parse_time(run.get("updated_at")) or _parse_time(run.get("created_at")),
        resolved=False,
        labels={"workflow": name, "branch": branch, "conclusion": conclusion},
    )


def signal_from_workflow_recovery(payload: dict[str, Any]) -> Signal | None:
    """The same workflow going green again — the resolved edge of the above."""
    run = payload.get("workflow_run") or {}
    if not isinstance(run, dict) or str(run.get("conclusion") or "").lower() != "success":
        return None
    failing = signal_from_workflow_run({**payload, "workflow_run": {**run, "conclusion": "failure"}})
    if failing is None:
        return None
    return Signal(
        repo=failing.repo,
        origin=Origin.CI,
        title=failing.title,
        fingerprint=failing.fingerprint,
        severity=failing.severity,
        note=f"recovered: run={run.get('html_url') or run.get('id')}",
        at=_parse_time(run.get("updated_at")) or failing.at,
        resolved=True,
        labels=failing.labels,
    )
