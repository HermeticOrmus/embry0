"""Renamed-repo URL normalisation (#277)."""

import pytest

from embry0.storage import repo_renames
from embry0.storage.repo_renames import REPO_RENAMES, normalize_repo_slug, normalize_repo_url

# The product ships no operator rename. Behaviour tests pass a local map.
RENAMES = {"acme/old": "acme/new"}

OLD = "https://github.com/acme/old"
NEW = "https://github.com/acme/new"

# (stored, expected); None means the URL passes through untouched.
CASES = [
    (f"{OLD}/pull/412", f"{NEW}/pull/412"),
    (f"{OLD}/issues/7", f"{NEW}/issues/7"),
    (OLD, NEW),
    (f"{OLD}/", f"{NEW}/"),
    (f"{OLD}?tab=readme", f"{NEW}?tab=readme"),
    (f"{OLD}/pull/9#issuecomment-1", f"{NEW}/pull/9#issuecomment-1"),
    ("http://github.com/acme/old/pull/1", "http://github.com/acme/new/pull/1"),
    (
        "https://www.github.com/acme/old/pull/1",
        "https://www.github.com/acme/new/pull/1",
    ),
    # GitHub slugs are case-insensitive.
    ("https://github.com/Acme/Old/pull/3", f"{NEW}/pull/3"),
    # Already current, or not in the map: untouched.
    (f"{NEW}/pull/412", f"{NEW}/pull/412"),
    ("https://github.com/acme/widgets/pull/1", "https://github.com/acme/widgets/pull/1"),
    # The slug must end at a path boundary — a longer repo name is another repo.
    (f"{OLD}-frontend/pull/1", f"{OLD}-frontend/pull/1"),
    (f"{OLD}.wiki/pull/1", f"{OLD}.wiki/pull/1"),
    # Only a repo URL is rewritten, not any text that mentions the slug.
    ("https://linear.app/acme/issue/AC-1?x=github.com/acme/old/", None),
    ("https://gitlab.com/acme/old/pull/1", None),
    ("https://github.com/other/acme/old", None),
]


def _expected(stored: str, expected: str | None) -> str:
    return stored if expected is None else expected


def test_shipped_renames_are_empty():
    """Nothing is renamed until the operator configures REPO_RENAMES."""
    assert REPO_RENAMES == {}


def test_omitted_mapping_reads_the_shipped_map_at_call_time(monkeypatch):
    """Callers omit the map. The default must see a replacement installed
    after import, which is how merge sync and healing are tested."""
    stored = "https://github.com/acme/old/pull/1"
    assert normalize_repo_url(stored) == stored
    monkeypatch.setattr(repo_renames, "REPO_RENAMES", RENAMES)
    assert normalize_repo_url(stored) == "https://github.com/acme/new/pull/1"
    assert normalize_repo_slug("acme/old") == "acme/new"


# ---- REPO_RENAMES is operator config (#527) ---------------------------------
# The parser and the installer are reached through the module, so on a tree
# without them each test fails on its own instead of the whole file failing
# to import.


@pytest.fixture
def installed(monkeypatch):
    """Restore the process-wide map after a test installs one."""
    monkeypatch.setattr(repo_renames, "REPO_RENAMES", {})


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_an_unset_repo_renames_renames_nothing(raw):
    assert repo_renames.parse_repo_renames(raw) == ({}, [])


def test_repo_renames_is_a_json_object_of_slugs():
    raw = '{"acme/old": "acme/new", "Acme/Older": "acme/new"}'
    assert repo_renames.parse_repo_renames(raw) == ({"acme/old": "acme/new", "Acme/Older": "acme/new"}, [])


@pytest.mark.parametrize("raw", ["acme/old=acme/new", '["acme/old", "acme/new"]', '"acme/old"', "{"])
def test_a_value_that_is_not_a_json_object_renames_nothing(raw):
    renames, problems = repo_renames.parse_repo_renames(raw)
    assert renames == {}, f"{raw!r} must rename nothing"
    assert len(problems) == 1, f"{raw!r} must be reported once, got {problems!r}"


def test_an_entry_that_is_not_two_slugs_is_dropped_and_the_rest_kept():
    raw = (
        '{"acme/old": "acme/new", "old": "acme/a", "acme/b": "https://github.com/acme/b2",'
        ' "acme/c": "acme/c2\\n", "acme/d": 7, "acme/e\\\\x": "acme/e2"}'
    )
    renames, problems = repo_renames.parse_repo_renames(raw)
    assert renames == {"acme/old": "acme/new"}, f"only the one valid pair may survive, got {renames!r}"
    assert len(problems) == 5, f"each refused entry must be reported, got {problems!r}"


def test_a_chained_rename_is_refused():
    """a -> b -> c would make normalisation stop being idempotent: the entry
    that points at a renamed slug is dropped and named, its successor kept."""
    renames, problems = repo_renames.parse_repo_renames('{"acme/a": "acme/b", "acme/b": "acme/c"}')
    assert renames == {"acme/b": "acme/c"}, f"the chained entry must be dropped, got {renames!r}"
    assert len(problems) == 1 and "'acme/a'" in problems[0], f"the chain must be reported, got {problems!r}"


def test_a_case_only_rename_is_not_a_chain():
    assert repo_renames.parse_repo_renames('{"Acme/Widgets": "acme/widgets"}') == ({"Acme/Widgets": "acme/widgets"}, [])


def test_configure_installs_the_map_every_caller_reads(installed):
    import structlog

    with structlog.testing.capture_logs() as logs:
        got = repo_renames.configure_repo_renames('{"acme/old": "acme/new", "old": "acme/x"}')
    assert got == RENAMES
    assert normalize_repo_url("https://github.com/acme/old/pull/1") == "https://github.com/acme/new/pull/1", (
        "callers that omit the map must see the configured renames"
    )
    assert normalize_repo_slug("acme/old") == "acme/new"
    errors = [e for e in logs if e["log_level"] == "error"]
    assert [e["event"] for e in errors] == ["repo_renames_invalid"], f"the bad entry must be logged, got {logs!r}"


def test_configure_with_nothing_set_clears_an_earlier_map(installed):
    repo_renames.configure_repo_renames('{"acme/old": "acme/new"}')
    assert repo_renames.configure_repo_renames("") == {}
    assert normalize_repo_url("https://github.com/acme/old/pull/1") == "https://github.com/acme/old/pull/1"


@pytest.mark.parametrize(("stored", "expected"), CASES)
def test_normalize_repo_url(stored: str, expected: str | None):
    assert normalize_repo_url(stored, RENAMES) == _expected(stored, expected)


def test_normalize_repo_url_is_idempotent():
    once = normalize_repo_url(f"{OLD}/pull/1", RENAMES)
    assert normalize_repo_url(once, RENAMES) == once


@pytest.mark.parametrize("empty", [None, ""])
def test_normalize_repo_url_passes_empties_through(empty):
    assert normalize_repo_url(empty, RENAMES) == empty


def test_normalize_repo_url_takes_an_explicit_mapping():
    assert normalize_repo_url("https://github.com/acme/old/pull/1", RENAMES) == "https://github.com/acme/new/pull/1"
    assert (
        normalize_repo_url("https://github.com/other/widgets/pull/1", RENAMES)
        == "https://github.com/other/widgets/pull/1"
    )


def test_normalize_repo_slug():
    assert normalize_repo_slug("acme/old", RENAMES) == "acme/new"
    assert normalize_repo_slug("Acme/Old", RENAMES) == "acme/new"
    assert normalize_repo_slug("acme/new", RENAMES) == "acme/new"
    assert normalize_repo_slug("acme/old-frontend", RENAMES) == "acme/old-frontend"


def test_repo_url_variants_cover_the_old_slug_either_way_round():
    from embry0.storage.repo_renames import repo_url_variants

    expected = [
        "https://github.com/acme/new/pull/418",
        "https://github.com/acme/old/pull/418",
    ]
    assert repo_url_variants("https://github.com/acme/new/pull/418", RENAMES) == expected
    assert repo_url_variants("https://github.com/acme/old/pull/418", RENAMES) == expected
    # a repo that was never renamed, and a look-alike, have exactly one form
    assert repo_url_variants("https://github.com/acme/widgets/pull/1", RENAMES) == [
        "https://github.com/acme/widgets/pull/1"
    ]
    lookalike = "https://github.com/acme/new-frontend/pull/2"
    assert repo_url_variants(lookalike, RENAMES) == [lookalike]


def test_repo_slug_variants():
    from embry0.storage.repo_renames import repo_slug_variants

    assert repo_slug_variants("acme/old", RENAMES) == ["acme/new", "acme/old"]
    assert repo_slug_variants("Acme/New", RENAMES) == ["Acme/New", "acme/old"]
    assert repo_slug_variants("acme/widgets", RENAMES) == ["acme/widgets"]
