"""The confirm-healed sweep (#312, SH-P5.4) — the impure half of `confirm`.

Reads the cases whose fix is running, asks `log_signatures` what their
signature has done since the deploy marker, records a verdict and comments on
the ticket. Nothing else: the plan calls this stage "read-only apart from
ticket comments", which is why it is the one autonomy stage that ships without
a shadow period.

Three properties worth keeping:

* **Idempotent.** Every write carries an ``event_key`` derived from the case,
  its deploy anchor and the verdict, so a sweep that runs twice over the same
  state writes once. The ledger drops a repeated ``event_key`` whole.
* **Re-runnable.** A verdict is not final. `record_deploy` deliberately resets
  a recorded verdict to ``pending`` when an earlier deploy turns up late
  (`anchor_replaced_after_verdict`), and this sweep will then judge it again
  against the corrected anchor. Nothing here assumes one shot.
* **Fail-open per case.** One unreadable case must not stop the sweep: the
  remaining cases are still judged and the failure is logged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from embry0.services.healing.confirm import Judgement, Readiness, judge, plan_verification
from embry0.services.healing.lifecycle import Verdict
from embry0.services.loop_schedule import seconds_until_next_run

logger = structlog.get_logger(__name__)

#: A not-healed verdict is retried once with fresh data before a human is
#: asked — the plan's "one retry, then human". Counted from the case's own
#: history rather than kept in memory, so a restart does not reset it.
MAX_NOT_HEALED_RETRIES = 1


def _evidence_minutes(evidence: dict[str, object], key: str) -> int:
    """Whole minutes in one of the evidence's seconds fields, which judge() writes as int."""
    seconds = evidence.get(key, 0)
    return int(seconds) // 60 if isinstance(seconds, int | float) else 0


class _Cases(Protocol):
    async def awaiting_verdict(self, *, repo: str | None = ..., limit: int = ...) -> list[dict[str, Any]]: ...


class _Signatures(Protocol):
    async def get(self, signature_id: str, *, repo: str | None = ...) -> Any: ...


class ConfirmHealedSweep:
    """Judge every case whose fix has shipped."""

    def __init__(
        self,
        *,
        cases_repo: _Cases,
        cases_service: Any,
        signatures_repo: _Signatures,
        config: Any,
        commenter: Any = None,
        metrics: Any = None,
    ) -> None:
        self._repo = cases_repo
        self._cases = cases_service
        self._signatures = signatures_repo
        self._config = config
        self._commenter = commenter
        self._metrics = metrics

    @property
    def enabled(self) -> bool:
        """On unless explicitly turned off.

        The inverse of every other stage's default, and deliberately: this one
        only reads and comments, and the plan says to build it early and leave
        it on. A stage that cannot act wrongly does not need a shadow period.
        """
        return bool(getattr(self._config, "confirm_healed_enabled", True))

    @property
    def interval_seconds(self) -> float:
        return float(getattr(self._config, "confirm_healed_interval_seconds", 1800))

    async def run_once(self) -> dict[str, int]:
        """One pass. Returns a tally by outcome, for the log line and tests."""
        tally = {"checked": 0, "healed": 0, "not_healed": 0, "inconclusive": 0, "too_early": 0, "failed": 0}
        repo = getattr(self._config, "watcher_repo", "") or None
        try:
            cases = await self._repo.awaiting_verdict(repo=repo)
        except Exception:
            logger.warning("confirm_healed_cases_unreadable", exc_info=True)
            return tally

        for case in cases:
            tally["checked"] += 1
            try:
                outcome = await self._judge_case(case)
            except Exception:
                # One bad case must not cost the rest of the sweep its pass.
                logger.warning("confirm_healed_case_failed", case_id=case.get("id"), exc_info=True)
                tally["failed"] += 1
                continue
            tally[outcome] = tally.get(outcome, 0) + 1

        logger.info("confirm_healed_sweep", **tally)
        return tally

    async def _judge_case(self, case: dict[str, Any]) -> str:
        case_id = str(case["id"])
        # deploy_marker_at is when the deploy was OBSERVED; deployed_at is when
        # the ledger recorded it. The marker is the truer anchor — it is the
        # deploy host's own timestamp — so prefer it and fall back.
        deployed_at: datetime | None = case.get("deploy_marker_at") or case.get("deployed_at")
        if deployed_at is None:
            return "too_early"

        signature_id = case.get("signature_id")
        row = None
        if signature_id:
            try:
                row = await self._signatures.get(str(signature_id), repo=case.get("repo"))
            except Exception:
                # Unreadable is not the same as absent: the counter may be back
                # next pass, so wait rather than record a verdict on nothing.
                logger.warning("confirm_healed_signature_unreadable", case_id=case_id, exc_info=True)
                return "too_early"

        prior_rate = getattr(row, "rate_per_hour", None) if row is not None else None
        plan = plan_verification(
            deployed_at=deployed_at,
            now=datetime.now(UTC),
            prior_rate_per_hour=prior_rate,
        )
        # A case with no signature can never be counted, so waiting out a
        # seven-day window would only delay the same answer. Say so now.
        if plan.readiness is Readiness.TOO_EARLY and signature_id:
            logger.debug("confirm_healed_too_early", case_id=case_id, reason=plan.reason)
            return "too_early"

        last_seen = getattr(row, "last_seen_at", None) if row is not None else None
        seen_since = bool(last_seen and last_seen > deployed_at)
        judgement = judge(
            signature_id=str(signature_id) if signature_id else None,
            repo=str(case.get("repo") or ""),
            seen_since_deploy=seen_since,
            occurrences_after_at_least=int(getattr(row, "last_window_count", 0) or 0),
            prior_rate_per_hour=prior_rate,
            plan=plan,
            last_seen_at=last_seen,
            deployed_at=deployed_at,
        )

        # Keyed on the deploy anchor and the verdict, so a second sweep over
        # unchanged state writes nothing, while a re-judgement after the anchor
        # was replaced is a genuinely new event.
        event_key = f"confirm:{case_id}:{deployed_at.isoformat()}:{judgement.verdict.value}"
        await self._cases.record_verdict(
            case_id,
            judgement.verdict,
            evidence=judgement.evidence,
            event_key=event_key,
        )
        logger.info(
            "confirm_healed_verdict",
            case_id=case_id,
            verdict=judgement.verdict.value,
            summary=judgement.summary,
            window_s=judgement.evidence.get("window_s"),
        )
        if self._metrics is not None:
            getattr(self._metrics, "case_verdict", lambda *_a, **_k: None)(judgement.verdict.value)
        await self._comment(case, judgement)
        return judgement.verdict.value

    async def _comment(self, case: dict[str, Any], judgement: Judgement) -> None:
        """Say so on the ticket. The only thing this sweep writes outside the
        ledger, and the reason it needs no shadow period.

        Only a real answer is posted. `inconclusive` stays in the ledger: on
        the first live run all five verdicts were "no signature to verify
        against" on historical tickets, and a comment saying *we could not
        tell* on each one is noise a human then has to read.
        """
        if self._commenter is None or not case.get("tracker_key"):
            return
        if judgement.verdict not in (Verdict.HEALED, Verdict.NOT_HEALED):
            return
        verdict = judgement.verdict
        heading = {
            Verdict.HEALED: "✅ Confirmed healed",
            Verdict.NOT_HEALED: "❌ Not healed",
            Verdict.INCONCLUSIVE: "➖ Inconclusive",
        }.get(verdict, verdict.value)
        body = (
            f"**{heading}** — {judgement.summary}\n\n"
            f"Checked {_evidence_minutes(judgement.evidence, 'elapsed_s')} minutes after the deploy"
            f" ({case.get('deploy_sha') or 'unknown sha'}), against a window of"
            f" {_evidence_minutes(judgement.evidence, 'window_s')} minutes sized to the signature's"
            f" own prior rate.\n\n"
            f"<sub>embry0 confirm-healed (#312) · case `{case['id']}`</sub>"
        )
        try:
            await self._commenter(str(case["tracker_key"]), body)
        except Exception:
            # A tracker that will not take a comment must not undo a recorded
            # verdict: the ledger is the record, the comment is the courtesy.
            logger.warning("confirm_healed_comment_failed", case_id=case.get("id"), exc_info=True)

    # ---- the loop ---------------------------------------------------------

    async def start(self) -> None:  # pragma: no cover - exercised by the app
        import asyncio

        last_run: datetime | None = None
        first = True
        while True:
            delay = seconds_until_next_run(
                now=datetime.now(UTC),
                interval_seconds=self.interval_seconds,
                min_delay_seconds=float(getattr(self._config, "confirm_healed_startup_delay_seconds", 120))
                if first
                else 0.0,
                last_run_at=last_run,
            )
            first = False
            await asyncio.sleep(delay)
            try:
                await self.run_once()
            except Exception:
                logger.warning("confirm_healed_sweep_failed", exc_info=True)
            last_run = datetime.now(UTC)


def linear_commenter(linear_sync: Any, team_id: str) -> Any:
    """A commenter that posts to Linear by tracker key.

    The ledger stores `tracker_key` ("RAV-1466") because that is what a person
    reads, but Linear's `commentCreate` needs the issue's UUID. The first live
    run passed the key straight to `post_issue_comment`, which expects a
    TrackerIssueRef, and every comment died with an AttributeError. This does
    the lookup, and raises on any failure so the sweep logs it — the verdict is
    already recorded by then, so a failed comment never undoes it.
    """

    async def _comment(tracker_key: str, body: str) -> None:
        native = (await linear_sync.native_ids(team_id, [tracker_key])).get(tracker_key)
        if not native:
            raise LookupError(f"no Linear issue found for {tracker_key}")
        if not await linear_sync.post_comment(native, body):
            raise RuntimeError(f"Linear rejected the comment on {tracker_key}")

    return _comment
