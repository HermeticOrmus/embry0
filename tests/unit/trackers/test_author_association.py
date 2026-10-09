"""The one author_association allowlist both inbound comment paths share (#253)."""

import pytest

from embry0.config import Embry0Config
from embry0.trackers.author_association import (
    DEFAULT_AUTHOR_ROLES,
    check_comment_author,
    parse_author_roles,
)

ALLOWED = parse_author_roles(DEFAULT_AUTHOR_ROLES)


def test_config_default_is_the_shared_constant():
    """The gate and the knob that configures it must not drift apart —
    that drift is what let /answer run ungated while dispatch did not."""
    assert Embry0Config(_env_file=None).comment_dispatch_author_roles == DEFAULT_AUTHOR_ROLES


def test_parse_normalises_case_and_whitespace():
    assert parse_author_roles(" owner , Member ") == {"OWNER", "MEMBER"}


@pytest.mark.parametrize("raw", [None, "", "   ", ",,"])
def test_parse_of_an_empty_allowlist_denies_everyone(raw):
    assert parse_author_roles(raw) == set()
    allowed, _ = check_comment_author({"author_association": "OWNER"}, parse_author_roles(raw))
    assert allowed is False


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "collaborator"])
def test_trusted_associations_pass(association):
    assert check_comment_author({"author_association": association}, ALLOWED) == (True, "ok")


@pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", ""])
def test_untrusted_associations_are_rejected_with_a_reason(association):
    allowed, reason = check_comment_author({"author_association": association}, ALLOWED)
    assert allowed is False
    assert reason.startswith("author_association_")


def test_missing_association_is_rejected():
    allowed, reason = check_comment_author({}, ALLOWED)
    assert (allowed, reason) == (False, "author_association_missing_not_allowed")


def test_bots_are_rejected_before_the_association_is_consulted():
    comment = {"author_association": "OWNER", "user": {"login": "embry0[bot]"}}
    assert check_comment_author(comment, ALLOWED) == (False, "bot_author")


def test_a_user_typed_bot_is_rejected_even_without_the_login_suffix():
    """GitHub marks App and Actions comments with `user.type == "Bot"`; the
    `[bot]` login suffix is a convention, not a guarantee. embry0 posting
    under a PAT identity that answers its own question is the loop this
    closes (#253)."""
    comment = {"author_association": "OWNER", "user": {"login": "embry0-ci", "type": "Bot"}}
    assert check_comment_author(comment, ALLOWED) == (False, "bot_author")


def test_a_human_user_type_still_passes():
    comment = {"author_association": "OWNER", "user": {"login": "alex", "type": "User"}}
    assert check_comment_author(comment, ALLOWED) == (True, "ok")
