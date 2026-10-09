"""Label values stay bounded whatever the data does (#285)."""

from embry0.metrics.labels import (
    OTHER,
    UNCATEGORISED,
    BoundedLabel,
    ErrorLabels,
    enum_label,
    model_family,
    provider_label,
    resolve_error_categoriser,
)
from embry0.services.healing.lifecycle import Stage


def test_bounded_label_overflows_to_other_and_keeps_known_values() -> None:
    label = BoundedLabel(("unassigned",), name="pipeline_template", limit=3)
    assert [label(f"tpl-{i}") for i in range(5)] == ["tpl-0", "tpl-1", "tpl-2", OTHER, OTHER]
    assert label("tpl-1") == "tpl-1"  # an admitted value stays admitted
    assert label("unassigned") == "unassigned"  # known values never count against the limit


def test_bounded_label_normalises_free_text() -> None:
    label = BoundedLabel(name="reason", limit=5)
    assert label("State:Backlog") == "state:backlog"
    assert label("Dispatch FAILED: see /app/x.py!") == "dispatch_failed:_see_app_x.py"
    assert label(None) == "none"
    assert label("   ") == "none"
    assert len(label("x" * 500)) == 64


def test_the_first_overflow_is_logged_once_and_names_the_label() -> None:
    """The ceiling is invisible in the metric — `other` looks like a value —
    so an operator only learns the label stopped growing from this line."""
    from structlog.testing import capture_logs

    label = BoundedLabel(name="pipeline_template", limit=1)
    with capture_logs() as logs:
        assert label("first") == "first"
        assert label("second") == OTHER
        assert label("third") == OTHER
        assert label("second") == OTHER

    assert [entry["event"] for entry in logs] == ["metric_label_overflow"]
    assert logs[0]["label"] == "pipeline_template"
    assert logs[0]["limit"] == 1
    assert logs[0]["value"] == "second"


def test_enum_label_refuses_unknown_values() -> None:
    assert enum_label(Stage, "tracked") == "tracked"
    assert enum_label(Stage, Stage.MERGED) == "merged"
    assert enum_label(Stage, "job-123abc") == OTHER
    assert enum_label(Stage, None) == "none"


def test_model_family_drops_the_version() -> None:
    assert model_family("claude-opus-5") == "claude-opus"
    assert model_family("claude-sonnet-5") == "claude-sonnet"
    assert model_family("claude-haiku-4-5") == "claude-haiku"
    assert model_family("claude-3-unknown") == "claude-other"
    assert model_family("grok-4.5") == "grok"
    assert model_family("gpt-9") == OTHER
    assert model_family(None) == OTHER


def test_provider_label() -> None:
    assert provider_label("claude-opus-5") == "anthropic"
    assert provider_label("grok-4.5") == "xai"
    assert provider_label("gpt-9") == OTHER


def test_the_taxonomy_owns_the_category_vocabulary_including_a_missing_code() -> None:
    """One vocabulary across these labels, /stats.failure_categories and the
    alert rules: a missing or unrecognised code is the taxonomy's `unknown`
    (#340), never a value this module invents. Only `error_code` says `none`,
    because a completed job genuinely carries no code."""
    categorise = {"ERR_SANDBOX_INIT": "infra"}.get
    labels = ErrorLabels(lambda code: categorise(code or "", "unknown"))
    assert labels(None) == ("none", "unknown")
    assert labels("") == ("none", "unknown")
    assert labels("ERR_SANDBOX_INIT") == ("ERR_SANDBOX_INIT", "infra")
    assert labels("ERR_UNKNOWN") == ("ERR_UNKNOWN", "unknown")


def test_the_real_taxonomy_is_what_gets_resolved_and_labelled() -> None:
    """End to end against the shipped taxonomy (#352), not a stand-in: the
    label vocabulary #286 writes against is whatever `category_for_code`
    says, and a missing or unrecognised code is its `unknown`."""
    from embry0.safety.error_codes import FailureCategory, category_for_code

    assert resolve_error_categoriser() is category_for_code

    labels = ErrorLabels(resolve_error_categoriser())
    assert labels("ERR_BUDGET_OVERRUN") == ("ERR_BUDGET_OVERRUN", "budget")
    assert labels("ERR_SANDBOX_INIT") == ("ERR_SANDBOX_INIT", "infra")
    # No code at all, and a code from a newer deploy: both the taxonomy's own
    # `unknown`, never this module's `uncategorised`.
    assert labels(None) == ("none", "unknown")
    assert labels("ERR_FROM_A_NEWER_DEPLOY") == (OTHER, "unknown")
    # Every category fits under the error_category ceiling, so none of them
    # can ever be squeezed out to `other`.
    assert len(FailureCategory) <= 16


def test_uncategorised_means_this_build_has_no_taxonomy_to_ask() -> None:
    """Distinct from the taxonomy's `unknown`: nobody was asked, as opposed to
    asked and unable to place it."""
    labels = ErrorLabels(None)
    assert labels(None) == ("none", UNCATEGORISED)
    assert labels("ERR_BUDGET_OVERRUN") == ("ERR_BUDGET_OVERRUN", UNCATEGORISED)
    assert labels("Traceback: boom at /app/x.py") == (OTHER, UNCATEGORISED)


def test_error_labels_use_a_categoriser_when_the_taxonomy_provides_one() -> None:
    from enum import StrEnum

    class Category(StrEnum):
        INFRA = "infra"

    labels = ErrorLabels(lambda code: Category.INFRA if code == "ERR_SANDBOX_INIT" else None)
    assert labels("ERR_SANDBOX_INIT") == ("ERR_SANDBOX_INIT", "infra")
    # A categoriser that answers with nothing at all is a broken categoriser,
    # not a verdict — it cannot mean the taxonomy placed this code.
    assert labels("ERR_UNKNOWN") == ("ERR_UNKNOWN", UNCATEGORISED)


def test_an_enum_class_of_the_same_name_is_not_mistaken_for_a_categoriser() -> None:
    """A FailureCategory enum is callable but is a constructor: it would raise
    ValueError on every real code and silently mark everything uncategorised."""
    from enum import StrEnum
    from types import SimpleNamespace
    from unittest.mock import patch

    class FailureCategory(StrEnum):
        INFRA = "infra"

    module = SimpleNamespace(error_category=FailureCategory, category_for_code=lambda code: FailureCategory.INFRA)
    with patch.dict("sys.modules", {"embry0.safety": SimpleNamespace(error_codes=module)}):
        from embry0.metrics.labels import resolve_error_categoriser

        resolved = resolve_error_categoriser()
    assert resolved is not None
    assert resolved("ERR_SANDBOX_INIT") == FailureCategory.INFRA


def test_error_labels_survive_a_categoriser_that_raises() -> None:
    def boom(_code: str) -> str:
        raise KeyError("nope")

    assert ErrorLabels(boom)("ERR_UNKNOWN") == ("ERR_UNKNOWN", UNCATEGORISED)
