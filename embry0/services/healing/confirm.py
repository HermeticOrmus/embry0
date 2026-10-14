"""Did the fix actually work? (#312, SH-P5.4)

The loop ended at "merged". Nothing ever checked that the error stopped, so
RAV-419 closed `LazyInitializationException` while it was still running at
22-82 lines a day, and RAV-1985's fix genuinely worked (545/day -> 0) but only
a human auditing by hand ever found that out. Of 26-33 merged fixes, **zero**
had a recorded healed / not-healed outcome.

Two halves, as everywhere else here. This module is the pure one: given what a
signature did before a fix shipped and what it has done since, decide the
verdict. No clock, no Loki, no database — so the window arithmetic below, which
is the part that is easy to get quietly wrong, is testable.

**Why the observation window is scaled, not fixed.** A signature running at
500/hour proves itself in fifteen minutes; one running at 3/day proves nothing
in a week. A fixed window would call the second one healed on the strength of
never having had time to fire — the most dangerous wrong answer available,
because it closes the ticket. So the window is sized from the signature's own
prior rate: long enough that, if the fault were still live, we would expect to
have seen it several times over. When even that is impractical the verdict is
``inconclusive``, which is an honest answer and the one the plan asks for.

**Why counting uses `log_signatures` and not the case's `signature` field.**
The ledger's `signature` column holds a human-readable signature — a cluster
template for a watcher finding, a PromQL expression for an alert (#296). It is
NOT LogQL and must not be executed. The durable per-signature counts that #289
and #369 put in `log_signatures` are the real record of what a signature has
done, keyed on `(signature_id, repo)`, and they are what this reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from embry0.services.healing.lifecycle import SignatureSource, Verdict

#: Occurrences we would expect to see in the observation window if the fault
#: were still live. Three is the smallest number that is not comfortably
#: explained by chance for a Poisson-ish arrival process, and the verdict only
#: has to be good enough to route a human's attention.
EXPECTED_OCCURRENCES = 3.0

#: Never wait less than this before judging, however loud the signature. A
#: deploy takes time to roll through, and the seconds right after a restart are
#: the least representative minutes the service will have.
MIN_WINDOW = timedelta(minutes=15)

#: Never wait longer than this. Past it, say `inconclusive` and stop holding
#: the case open: a signature this rare is not going to be settled by waiting,
#: and an honest "cannot tell" is more useful than an indefinite pending.
MAX_WINDOW = timedelta(days=7)

#: Below this lifetime rate a signature is too rare to judge at all.
#: One per day over MAX_WINDOW is 7 occurrences — already marginal.
MIN_JUDGEABLE_RATE_PER_HOUR = EXPECTED_OCCURRENCES / MAX_WINDOW.total_seconds() * 3600.0


class Readiness(StrEnum):
    """Whether a case can be judged yet."""

    READY = "ready"  # the window has elapsed; judge it
    TOO_EARLY = "too_early"  # come back later
    UNJUDGEABLE = "unjudgeable"  # too rare to ever settle


@dataclass(frozen=True)
class VerificationPlan:
    """When this case can be judged, and on what evidence."""

    readiness: Readiness
    window: timedelta
    elapsed: timedelta
    prior_rate_per_hour: float | None
    reason: str

    @property
    def ready(self) -> bool:
        return self.readiness is Readiness.READY


def observation_window(prior_rate_per_hour: float | None) -> timedelta:
    """How long to watch a signature running at this rate before judging.

    The time in which we would expect :data:`EXPECTED_OCCURRENCES`, clamped to
    [:data:`MIN_WINDOW`, :data:`MAX_WINDOW`]. An unknown rate gets the maximum:
    when we do not know how often it fired, the only safe assumption is rarely.
    """
    if not prior_rate_per_hour or prior_rate_per_hour <= 0:
        return MAX_WINDOW
    hours = EXPECTED_OCCURRENCES / prior_rate_per_hour
    return max(MIN_WINDOW, min(MAX_WINDOW, timedelta(hours=hours)))


def plan_verification(
    *,
    deployed_at: datetime,
    now: datetime,
    prior_rate_per_hour: float | None,
) -> VerificationPlan:
    """Can this case be judged yet?"""
    elapsed = max(now - deployed_at, timedelta(0))
    window = observation_window(prior_rate_per_hour)

    if prior_rate_per_hour is not None and 0 < prior_rate_per_hour < MIN_JUDGEABLE_RATE_PER_HOUR:
        return VerificationPlan(
            Readiness.UNJUDGEABLE,
            window,
            elapsed,
            prior_rate_per_hour,
            (
                f"signature ran at {prior_rate_per_hour:.4f}/h before the fix; "
                f"even {MAX_WINDOW.days} days would not expect {EXPECTED_OCCURRENCES:.0f} occurrences"
            ),
        )
    if elapsed < window:
        return VerificationPlan(
            Readiness.TOO_EARLY,
            window,
            elapsed,
            prior_rate_per_hour,
            f"{_hms(elapsed)} since the deploy, need {_hms(window)}",
        )
    return VerificationPlan(
        Readiness.READY,
        window,
        elapsed,
        prior_rate_per_hour,
        f"{_hms(elapsed)} since the deploy, window {_hms(window)}",
    )


@dataclass(frozen=True)
class Judgement:
    """The verdict, and the numbers that produced it."""

    verdict: Verdict
    evidence: dict[str, object]
    summary: str


def judge(
    *,
    signature_id: str | None,
    repo: str,
    seen_since_deploy: bool,
    occurrences_after_at_least: int,
    prior_rate_per_hour: float | None,
    plan: VerificationPlan,
    last_seen_at: datetime | None = None,
    deployed_at: datetime | None = None,
    signature_source: str | None = None,
) -> Judgement:
    """Healed, not healed, or inconclusive — with the counts behind it.

    The verdict turns on ``seen_since_deploy``, which is **exact**: the watcher
    clusters contiguous windows, so ``log_signatures.last_seen_at`` moves
    forward the moment a signature reappears, and comparing it to the deploy
    marker answers "has this fired since the fix shipped?" without inventing
    anything. Zero over a window sized to expect several is healed; a fault
    signature has no tolerance band, so *any* reappearance is not-healed.

    ``occurrences_after_at_least`` is a **lower bound** — the last window's
    count for the signature. `log_signatures` accumulates a lifetime total and
    keeps no per-range history, so the exact number since the deploy is not
    recoverable; the evidence names the field accordingly rather than
    presenting a bound as a measurement.

    ``signature_source`` is how the watcher chose the signature (#657). A
    ``fallback`` signature is the window's loudest line, not one the case's
    evidence quotes: whether it stopped says nothing about this fix, so the
    verdict is ``inconclusive`` and the counts stay in the evidence for a
    person to read. ``None`` (any other origin, or a case opened before the
    source was recorded) is judged as before.

    The evidence dict is counts and rates only, never log lines: it is served
    by the API and the ledger refuses anything over 16 KiB.
    """
    expected = (prior_rate_per_hour or 0.0) * plan.window.total_seconds() / 3600.0
    evidence: dict[str, object] = {
        "signature_id": signature_id,
        "repo": repo,
        "window_s": int(plan.window.total_seconds()),
        "elapsed_s": int(plan.elapsed.total_seconds()),
        "before_rate_per_hour": round(prior_rate_per_hour, 4) if prior_rate_per_hour else None,
        "expected_if_unfixed": round(expected, 2),
        "seen_since_deploy": seen_since_deploy,
        # A lower bound, not a measurement — see the docstring.
        "after_at_least": occurrences_after_at_least if seen_since_deploy else 0,
        "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
        "deployed_at": deployed_at.isoformat() if deployed_at else None,
        "signature_source": signature_source,
        # Named so a reader knows the counts came from the signature ledger and
        # not from re-running a query against Loki.
        "source": "log_signatures",
    }

    if signature_id is None:
        evidence["reason"] = "the case carries no signature id, so nothing can be counted"
        return Judgement(Verdict.INCONCLUSIVE, evidence, "no signature to verify against")

    if signature_source == SignatureSource.FALLBACK:
        evidence["reason"] = (
            "the signature is the loudest line of the window the finding was detected in, not a line its "
            "evidence quotes, so the counts describe that line and not this case's error"
        )
        return Judgement(Verdict.INCONCLUSIVE, evidence, "the signature is not one the finding's evidence quotes")

    if plan.readiness is Readiness.UNJUDGEABLE:
        evidence["reason"] = plan.reason
        return Judgement(Verdict.INCONCLUSIVE, evidence, f"too rare to verify: {plan.reason}")

    if not seen_since_deploy:
        return Judgement(
            Verdict.HEALED,
            evidence,
            (
                f"not seen since the deploy. Before: {prior_rate_per_hour:.2f}/h"
                if prior_rate_per_hour
                else "not seen since the deploy"
            )
            + f" ({expected:.0f} expected over {_hms(plan.window)} if unfixed)",
        )

    return Judgement(
        Verdict.NOT_HEALED,
        evidence,
        (
            f"still firing: seen at least {occurrences_after_at_least} time(s) in the "
            f"{_hms(plan.elapsed)} since the deploy (was {prior_rate_per_hour or 0:.2f}/h)"
        ),
    )


def _hms(delta: timedelta) -> str:
    """A duration a human reads at a glance, for ticket comments."""
    total = int(max(delta.total_seconds(), 0))
    if total >= 86400:
        return f"{total / 86400:.1f}d"
    if total >= 3600:
        return f"{total / 3600:.1f}h"
    return f"{total / 60:.0f}m"
