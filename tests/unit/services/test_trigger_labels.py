"""Trigger-label normalisation and matching."""

from embry0.services.trigger_labels import matching_triggers, normalize_labels


def test_normalize_casefolds_and_strips():
    assert normalize_labels(["  Embry0 ", "AUTO-FIX"]) == {"embry0", "auto-fix"}


def test_normalize_drops_blanks_and_non_strings():
    assert normalize_labels(["embry0", "", "   ", None, 42, {"name": "x"}]) == {"embry0"}


def test_normalize_handles_none_and_empty():
    assert normalize_labels(None) == set()
    assert normalize_labels([]) == set()


def test_normalize_casefold_beats_lower_on_non_ascii():
    """casefold, not lower — forge labels are free text."""
    assert normalize_labels(["STRASSE", "straße"]) == {"strasse"}


def test_matching_is_case_insensitive_on_both_sides():
    """GitHub compared case-sensitively; an operator writing `Embry0` got silence."""
    assert matching_triggers(["Embry0"], ["embry0"]) == {"embry0"}
    assert matching_triggers(["embry0"], ["EMBRY0"]) == {"embry0"}


def test_matching_returns_only_the_intersection():
    assert matching_triggers(["bug", "embry0", "wontfix"], ["embry0", "auto-fix"]) == {"embry0"}


def test_matching_is_falsy_when_nothing_matches():
    assert not matching_triggers(["bug"], ["embry0"])
    assert not matching_triggers([], ["embry0"])
    assert not matching_triggers(["embry0"], [])
    assert not matching_triggers(None, None)
