"""Which labels dispatch a job — one definition, shared by every forge.

Before this module there were three disagreeing notions of "trigger label":
the ``TRIGGER_LABELS`` env var (the only one the GitHub webhook read), the
``integration_config.trigger_labels`` DB column (dashboard-editable, and
silently ignored on the webhook path), and a private ``DEFAULT_TRIGGER_LABEL``
constant in the Linear service that was not configurable at all.

Resolution lives in ``IntegrationConfigRepository.get_trigger_labels`` — DB row
wins, env fills the gap — matching the contract the sibling
``get_telegram_credentials`` / ``get_webhook_credentials`` getters already use.
This module owns the *matching* half: normalisation and comparison.

Matching is case-insensitive. GitHub compared case-sensitively and Linear
lower-cased one side only, so an operator who wrote ``Embry0`` on a GitHub
issue got silence. Forge label casing is not something an operator should have
to think about, and GitHub itself treats label names as case-insensitively
unique.
"""

from collections.abc import Iterable

__all__ = ["matching_triggers", "normalize_labels"]


def normalize_labels(labels: Iterable[str] | None) -> set[str]:
    """Casefold and strip labels, dropping blanks and non-strings.

    ``str.casefold`` rather than ``str.lower``: it handles non-ASCII label
    names correctly, and forge labels are free text.
    """
    if not labels:
        return set()
    return {label.strip().casefold() for label in labels if isinstance(label, str) and label.strip()}


def matching_triggers(labels: Iterable[str] | None, triggers: Iterable[str] | None) -> set[str]:
    """Return the normalised trigger labels present on ``labels``.

    Empty when nothing matches, which makes it directly truth-testable at call
    sites. Both sides are normalised, so the caller never has to care which
    forge produced the casing.
    """
    return normalize_labels(labels) & normalize_labels(triggers)
