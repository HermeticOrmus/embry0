"""Label values for the loop metrics — every one of them bounded.

A Prometheus series is created per distinct label value and never goes away,
so a label fed from data (a pipeline template name, a skip reason, a model id)
needs a ceiling that holds whatever the data does. Nothing here ever returns a
job id, ticket id, case id, url or error text.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from enum import StrEnum

import structlog

logger = structlog.get_logger(__name__)

OTHER = "other"
NONE = "none"

_SAFE = re.compile(r"[^a-z0-9_:.\-]+")
_MAX_VALUE_LENGTH = 64


class BoundedLabel:
    """Admits at most ``limit`` distinct values; everything after is ``other``.

    ``known`` values are always admitted and do not count against the limit of
    values learned at runtime. A value is lower-cased and reduced to a
    conservative character set first, so free text cannot mint series by
    varying punctuation or case.

    The limit is permanent for the process: once reached, every new value
    reads ``other`` until a restart. That is invisible in the metric itself —
    ``other`` looks like any other series — so the first overflow logs
    ``metric_label_overflow`` once, naming the label and the value that hit
    the ceiling. A recurring line across restarts means the ceiling is too low
    for the data, not that the data is unbounded.
    """

    def __init__(self, known: Iterable[str] = (), *, name: str, limit: int = 24, empty: str = NONE) -> None:
        self._known = frozenset(known)
        self._learned: set[str] = set()
        self._name = name
        self._limit = limit
        self._empty = empty
        self._lock = threading.Lock()
        self._overflowed = False

    def __call__(self, value: object) -> str:
        if value is None or value == "":
            return self._empty
        text = _SAFE.sub("_", str(value).strip().lower())[:_MAX_VALUE_LENGTH].strip("_")
        if not text:
            return self._empty
        if text in self._known or text in self._learned:
            return text
        with self._lock:
            if len(self._learned) >= self._limit:
                first = not self._overflowed
                self._overflowed = True
            else:
                self._learned.add(text)
                return text
        if first:
            logger.warning(
                "metric_label_overflow",
                label=self._name,
                limit=self._limit,
                # The normalised value, which is bounded and free of ids by
                # construction — it is what the label WOULD have been.
                value=text,
                hint=f"every further {self._name} value reads 'other' until restart",
            )
        return OTHER


def enum_label(enum: type[StrEnum], value: object, *, empty: str = NONE) -> str:
    """``value`` if the enum knows it, else ``other`` (``empty`` for nothing)."""
    if value is None or value == "":
        return empty
    text = str(value.value if isinstance(value, StrEnum) else value)
    return text if text in enum._value2member_map_ else OTHER


def provider_label(model: str | None) -> str:
    """The provider that serves ``model``: ``anthropic``, a registered
    non-Anthropic provider's name, or ``other``."""
    from embry0.agents.providers import ANTHROPIC, provider_for_model

    if not model:
        return OTHER
    provider = provider_for_model(model)
    if provider is not None:
        return provider.name
    if any(model.startswith(prefix) for prefix in ANTHROPIC.model_prefixes):
        return ANTHROPIC.name
    return OTHER


_CLAUDE_TIERS = ("opus", "sonnet", "haiku")


def model_family(model: str | None) -> str:
    """A model id without its version: ``claude-opus``, ``claude-sonnet``,
    ``claude-haiku``, ``grok``, … — a handful of values however many model
    versions ship."""
    if not model:
        return OTHER
    lowered = model.lower()
    if lowered.startswith("claude-"):
        return next((f"claude-{tier}" for tier in _CLAUDE_TIERS if tier in lowered), "claude-other")
    if lowered.startswith("grok"):
        return "grok"
    return OTHER


# ONLY "embry0.safety.error_codes exposes no categoriser in this build".
# Since #352 it does, so this is a defensive value, not an expected one — a
# series carrying it means the taxonomy module changed shape. It never means
# "a failure nobody could place": that is the taxonomy's own `unknown`.
UNCATEGORISED = "uncategorised"

# The failure taxonomy's code -> category function, by name.
# ``category_for_code`` is the one #352 ships and what this resolves today;
# the rest are fallbacks kept because the lookup is by name. Resolved once at
# wiring time — without it every failure would read ``uncategorised`` and the
# ``error_code`` label would carry the detail on its own.
_CATEGORISER_NAMES = ("category_for_code", "error_category", "category_for", "categorise", "categorize")


def resolve_error_categoriser() -> Callable[[str | None], object] | None:
    from embry0.safety import error_codes

    for name in _CATEGORISER_NAMES:
        candidate = getattr(error_codes, name, None)
        # A class of the same name (a FailureCategory enum) is callable but is
        # a constructor, not a categoriser — it would raise on every code.
        if callable(candidate) and not isinstance(candidate, type):
            return candidate  # type: ignore[no-any-return]
    return None


class ErrorLabels:
    """``(error_code, error_category)`` label values for a finished job."""

    def __init__(self, categorise: Callable[[str | None], object] | None = None) -> None:
        self._categorise = categorise
        self._category = BoundedLabel((UNCATEGORISED,), name="error_category", limit=16, empty=NONE)

    def __call__(self, code: str | None) -> tuple[str, str]:
        """``(error_code, error_category)``.

        The two labels speak different vocabularies and are read differently.
        ``error_code`` is an ``ErrorCode`` value, or ``none`` when the job
        carries no code at all — every completed job. ``error_category`` is
        the failure taxonomy's, which is authoritative: a missing or
        unrecognised code is its ``unknown`` (#340 / #352), so one vocabulary
        spans these labels, ``/stats.failure_categories`` and the alert rules
        built on them. Read failures with ``status="failed"``, not by
        filtering on a category.
        """
        from embry0.safety.error_codes import ErrorCode

        label = enum_label(ErrorCode, code)
        if self._categorise is None:
            # The taxonomy is not in this build; the code label carries the detail.
            return label, UNCATEGORISED
        try:
            category = self._categorise(code)
        except Exception:
            return label, UNCATEGORISED
        value = getattr(category, "value", category)
        return label, self._category(value) if value else UNCATEGORISED
