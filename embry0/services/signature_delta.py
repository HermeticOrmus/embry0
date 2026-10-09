"""What changed in this window, per signature (#290 SH-P3.2, #291 SH-P3.3).

#289 made a window's signatures countable. This module answers the question a
single window still cannot: **what is different about this one?** It compares
the clusters a tick just built against what
:class:`~embry0.storage.repositories.log_signatures.LogSignaturesRepository`
remembers, and labels each signature.

Two consumers, one computation:

* **#290** attributes a proposal to a *primary* signature, so a case is keyed
  on the shape of the log line rather than on a hash of whatever the model
  called it that day. A reworded re-detection then lands on the existing case
  instead of opening a fourth ticket for `LazyInitializationException`.
* **#291** skips the paid analysis entirely when nothing is new, spiking or
  regressed.

Pure on purpose — no clock, no I/O, no database. The caller reads the prior
rows, passes them in, and gets a verdict it can log and test.

**Read before you write.** ``record_window`` replaces ``last_window_count``,
so the prior rows must be fetched *before* this window is folded in, exactly
as ``known_ids`` already had to be. Fold first and every signature reads as
steady, which is the quiet-failure shape this repo keeps paying for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from embry0.services.log_signatures import LogCluster
from embry0.storage.repositories.log_signatures import SignatureRow

#: How many times louder than its last window a signature must be to count as
#: spiking. 3x is deliberately coarse: log volume is bursty, and a 2x threshold
#: fires on ordinary traffic variation.
DEFAULT_SPIKE_FACTOR = 3.0

#: A spike also has to clear an absolute floor, or a signature going from one
#: line to three reads as a 3x emergency.
DEFAULT_SPIKE_MIN_COUNT = 10

#: A signature is "regressed" when it returns after this long silent. Set
#: against the watcher's own cadence: an hourly tick means a signature absent
#: for six hours genuinely stopped rather than merely missing a window.
DEFAULT_REGRESSION_SILENCE_SECONDS = 6 * 3600


class Change(StrEnum):
    """Why this signature is interesting, or that it is not."""

    NEW = "new"  # never recorded for this repo before
    SPIKING = "spiking"  # far louder than its previous window
    REGRESSED = "regressed"  # seen before, went quiet, came back
    STEADY = "steady"  # present and unremarkable


#: The changes that justify paying a model to look at the window.
NOTABLE: frozenset[Change] = frozenset({Change.NEW, Change.SPIKING, Change.REGRESSED})


@dataclass(frozen=True)
class SignatureChange:
    """One signature's verdict, with the numbers behind it."""

    signature_id: str
    template: str
    change: Change
    count: int
    previous_count: int | None
    silent_seconds: float | None

    @property
    def notable(self) -> bool:
        return self.change in NOTABLE


@dataclass(frozen=True)
class WindowDelta:
    """Every signature in one window, ranked, plus the headline."""

    changes: tuple[SignatureChange, ...]

    @property
    def notable_changes(self) -> tuple[SignatureChange, ...]:
        return tuple(c for c in self.changes if c.notable)

    @property
    def has_notable(self) -> bool:
        return any(c.notable for c in self.changes)

    @property
    def primary(self) -> SignatureChange | None:
        """The signature a proposal from this window is attributed to.

        The loudest **notable** signature, falling back to the loudest
        signature overall. Ranking is inherited from ``cluster_entries``, which
        sorts by count then template, so this is total and reproducible — an
        unstable choice here would key a case on a different signature each
        tick and defeat the whole point.

        Deliberately not "whatever the model said": the model names the
        finding, the logs name the signature, and #290 exists because the
        former is not stable across rewordings.
        """
        return next(iter(self.notable_changes), None) or next(iter(self.changes), None)

    def counts(self) -> dict[str, int]:
        """Per-change tallies, for the tick's log line and the metrics."""
        tally = {change.value: 0 for change in Change}
        for item in self.changes:
            tally[item.change.value] += 1
        return tally


def classify_window(
    clusters: Sequence[LogCluster],
    prior: Mapping[str, SignatureRow],
    *,
    window_start: datetime | None = None,
    spike_factor: float = DEFAULT_SPIKE_FACTOR,
    spike_min_count: int = DEFAULT_SPIKE_MIN_COUNT,
    regression_silence_seconds: float = DEFAULT_REGRESSION_SILENCE_SECONDS,
) -> WindowDelta:
    """Label every cluster in this window against what came before.

    ``prior`` maps signature id to the row as it stood **before** this window
    was recorded. A signature missing from ``prior`` is new.
    """
    changes: list[SignatureChange] = []
    for cluster in clusters:
        row = prior.get(cluster.signature_id)
        silent_seconds: float | None = None
        previous_count: int | None = None

        if row is None:
            change = Change.NEW
        else:
            previous_count = row.last_window_count
            if window_start is not None and row.last_seen_at is not None:
                silent_seconds = max((window_start - row.last_seen_at).total_seconds(), 0.0)
            if silent_seconds is not None and silent_seconds >= regression_silence_seconds:
                change = Change.REGRESSED
            elif (
                previous_count > 0
                and cluster.count >= spike_min_count
                and cluster.count >= previous_count * spike_factor
            ):
                change = Change.SPIKING
            else:
                change = Change.STEADY

        changes.append(
            SignatureChange(
                signature_id=cluster.signature_id,
                template=cluster.template,
                change=change,
                count=cluster.count,
                previous_count=previous_count,
                silent_seconds=silent_seconds,
            )
        )
    return WindowDelta(changes=tuple(changes))
