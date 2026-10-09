"""A provider failure that ended an agent run: its code, the provider's own
words, and — for a spent usage window — when the account is usable again (#466).

The Claude CLI reports a spent subscription window in up to three messages on
one run, and the agent SDK in the sandbox image surfaces each:

- a ``RateLimitEvent`` whose ``rate_limit_info`` has ``status="rejected"``
  for a named window, with ``resets_at`` in Unix seconds;
- a synthetic ``AssistantMessage`` (``model="<synthetic>"``) flagged
  ``error="rate_limit"`` — the same flag as a transient 429 — whose text is
  the sentence a person reads: "You're out of extra usage · resets Oct 5,
  12am (UTC)";
- an ``is_error`` ``ResultMessage`` (``subtype="success"``) carrying that
  sentence as ``result``.

The SDK then raises "Claude Code returned an error result: success", which
says nothing. These helpers turn the signals into one :class:`ProviderFailure`.
Everything takes ``now`` as a parameter, so the parsing is deterministic in
tests. This module is baked into the sandbox image with the executor: it may
import only the standard library and ``embry0.safety``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from embry0.safety.error_codes import USAGE_EXHAUSTED_PHRASES, ErrorCode, classify_agent_error

_QUOTA = ErrorCode.PROVIDER_QUOTA_EXHAUSTED.value
_RATE = ErrorCode.PROVIDER_RATE_LIMITED.value


@dataclass(frozen=True)
class ProviderFailure:
    """What one run's provider signals say, merged.

    ``text`` is the provider's own sentence when it gave one. ``resets_at`` is
    set only for ``ERR_PROVIDER_QUOTA_EXHAUSTED`` and is always aware UTC.
    ``from_text`` is False when ``text`` was composed here from structured
    fields, so a later message's real sentence replaces it.
    """

    code: str | None
    text: str
    resets_at: datetime | None = None
    from_text: bool = True
    structured_reset: bool = False


# --- the provider's sentence ----------------------------------------------

_PHRASE_RE = re.compile(
    "|".join(re.escape(phrase).replace("'", "['\u2019]") for phrase in USAGE_EXHAUSTED_PHRASES),
    re.IGNORECASE,
)
# Where a sentence that follows earlier text on the same line begins:
# "... Now the migration: You're out of extra usage · resets ...".
_SENTENCE_BREAKS = (": ", ". ", "! ", "? ")


def usage_exhausted_sentence(text: str | None) -> str | None:
    """The provider's out-of-usage sentence inside ``text``, or None when the
    text has no out-of-usage wording.

    The sentence runs from the start of its line — or from just after the
    last ``": "``/``". "`` before the wording, when earlier text shares the
    line — to the end of that line.
    """
    if not text:
        return None
    match = _PHRASE_RE.search(text)
    if match is None:
        return None
    at = match.start()
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    if end == -1:
        end = len(text)
    for separator in _SENTENCE_BREAKS:
        cut = text.rfind(separator, start, at)
        if cut != -1:
            start = max(start, cut + len(separator))
    return text[start:end].strip() or None


# --- the reset time -------------------------------------------------------

# The CLI's reset formats (CLI 2.1.x, en-US): a time when the reset is within
# a day ("resets 12:50am (UTC)", "resets 3pm (UTC)"), a month and day beyond
# that ("resets Oct 5, 12am (UTC)"), and the year too when it differs from
# the current one ("resets Jan 3, 2027, 12am (UTC)"). The zone in brackets is
# the sandbox's own; without one the time cannot be placed, so it is not read.
_RESET_RE = re.compile(
    r"\bresets\s+"
    r"(?:(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2}),\s*(?:(?P<year>\d{4}),\s*)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)\s*"
    r"\((?P<zone>[^()\s]+)\)",
    re.IGNORECASE,
)
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
    )
}
_UTC_NAMES = frozenset({"utc", "gmt", "etc/utc", "etc/gmt", "z", "zulu"})
# A year-less date names the next such date; Feb 29 can be up to 8 years out.
_YEAR_SEARCH = 9


def _zone(name: str) -> tzinfo | None:
    if name.lower() in _UTC_NAMES:
        return UTC
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        # Unknown name, or no tz database in this image: the time cannot be placed.
        return None


def parse_reset(text: str | None, *, now: datetime) -> datetime | None:
    """When the account resets, from a provider sentence like
    ``"... · resets Oct 5, 12am (UTC)"``, as an aware UTC datetime.

    A time with no date is the next occurrence of that time at or after
    ``now``; a month and day with no year is the next such date at or after
    ``now``. Anything that does not parse — no zone, an unknown zone, an
    impossible date — is None, never a guess. ``now`` must be aware.
    """
    if not text:
        return None
    match = _RESET_RE.search(text)
    if match is None:
        return None
    zone = _zone(match["zone"])
    if zone is None:
        return None
    hour12 = int(match["hour"])
    minute = int(match["minute"] or 0)
    if not 1 <= hour12 <= 12 or minute > 59:
        return None
    hour = hour12 % 12 + (12 if match["meridiem"].lower() == "pm" else 0)
    local_now = now.astimezone(zone)

    if match["month"] is None:
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate < local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    month = _MONTHS.get(match["month"].lower())
    if month is None:
        return None
    day = int(match["day"])
    years = [int(match["year"])] if match["year"] else range(local_now.year, local_now.year + _YEAR_SEARCH)
    for year in years:
        try:
            candidate = datetime(year, month, day, hour, minute, tzinfo=zone)
        except ValueError:
            continue
        if match["year"] or candidate >= local_now:
            return candidate.astimezone(UTC)
    return None


def epoch_to_utc(value: Any) -> datetime | None:
    """A ``resets_at`` from ``rate_limit_info`` (Unix seconds) as aware UTC."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


# --- one signal -----------------------------------------------------------


def failure_from_text(text: str, *, code: str | None, now: datetime, fallback_text: str = "") -> ProviderFailure:
    """A failure from a message's text and the code its structured flag gave.

    Out-of-usage wording overrides the flag: the CLI marks a spent window
    ``rate_limit``, the same as a transient 429, and only the text tells them
    apart. Without a flag the text classifier decides.
    """
    sentence = usage_exhausted_sentence(text)
    if sentence is not None:
        return ProviderFailure(_QUOTA, sentence, parse_reset(sentence, now=now))
    if code is None and text:
        classified = classify_agent_error(text)
        code = classified.value if classified is not None else None
    resets_at = parse_reset(text, now=now) if code == _QUOTA else None
    return ProviderFailure(code, text or fallback_text, resets_at)


def _continuing_on_overage(info: Any) -> bool:
    """True when the window is rejected but the account keeps serving from
    extra usage — the CLI does not stop, so the run has not failed."""
    if getattr(info, "overage_status", None) in ("allowed", "allowed_warning"):
        return True
    raw = getattr(info, "raw", None)
    return isinstance(raw, dict) and raw.get("isUsingOverage") is True


def failure_from_rate_limit_info(info: Any) -> ProviderFailure | None:
    """A failure from ``rate_limit_info``, or None when it is not one.

    ``rejected`` for a named window (five_hour, seven_day, overage, ...) is a
    spent usage window: ``ERR_PROVIDER_QUOTA_EXHAUSTED`` until it resets. The
    reset is the earlier of the window's and, when extra usage is rejected
    too, the extra-usage reset — the same choice the CLI's own message makes.
    A rejection naming no window stays a rate limit.
    """
    if getattr(info, "status", None) != "rejected" or _continuing_on_overage(info):
        return None
    window = getattr(info, "rate_limit_type", None)
    if not isinstance(window, str) or not window:
        return ProviderFailure(_RATE, "rate limit rejected", from_text=False)
    candidates = [epoch_to_utc(getattr(info, "resets_at", None))]
    if getattr(info, "overage_status", None) == "rejected":
        candidates.append(epoch_to_utc(getattr(info, "overage_resets_at", None)))
    known = [when for when in candidates if when is not None]
    resets_at = min(known) if known else None
    text = f"usage limit reached for the {window} window"
    if resets_at is not None:
        text += f" · resets {resets_at.isoformat()}"
    return ProviderFailure(_QUOTA, text, resets_at, from_text=False, structured_reset=resets_at is not None)


# --- the whole run --------------------------------------------------------


def merge(prior: ProviderFailure | None, new: ProviderFailure) -> ProviderFailure:
    """Fold one more signal from the same run into what is known.

    - code: the first one stands, except that a spent window outranks a rate
      limit — the same account can be reported both ways on one run;
    - text: the first provider sentence stands; text composed from structured
      fields gives way to a real one;
    - reset: ``rate_limit_info``'s outranks one read from text.
    """
    if prior is None:
        return new
    code = prior.code or new.code
    if new.code == _QUOTA and code == _RATE:
        code = _QUOTA
    merged = prior
    if new.text and (not prior.text or (not prior.from_text and new.from_text)):
        merged = replace(merged, text=new.text, from_text=new.from_text)
    if new.resets_at is not None and (prior.resets_at is None or (new.structured_reset and not prior.structured_reset)):
        merged = replace(merged, resets_at=new.resets_at, structured_reset=new.structured_reset)
    merged = replace(merged, code=code)
    if code != _QUOTA and merged.resets_at is not None:
        merged = replace(merged, resets_at=None, structured_reset=False)
    return merged
