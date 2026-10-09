"""The one repo-path rule, shared by every schema that names a repo (spec 3.5)."""

import pytest

from embry0.api.schemas.repo_path import REPO_PATH_PATTERN, validate_repo_path


@pytest.mark.parametrize(
    "value",
    [
        "owner/name",
        "acme/products/fbr",
        "acme/group/backend",
        "a.b/c-d_e/f.g",
        "0rg/9ame",
    ],
)
def test_accepts_two_or_more_segments(value):
    assert validate_repo_path(value) == value
    assert REPO_PATH_PATTERN.match(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "owner",
        "/name",
        "owner/",
        "owner//name",
        "owner/../name",
        "-owner/name",
        "owner/.hidden",
        "owner/na me",
        "owner/name?x=1",
    ],
)
def test_rejects_malformed_paths(value):
    with pytest.raises(ValueError, match="group/subgroup/name"):
        validate_repo_path(value)


def test_rejects_a_trailing_newline():
    """``\\Z``, not ``$`` — ``$`` matches before a trailing newline, and the
    repo string flows into clone URLs and sanitised container/volume names."""
    assert REPO_PATH_PATTERN.match("owner/name\n") is None
    with pytest.raises(ValueError, match="group/subgroup/name"):
        validate_repo_path("owner/name\n")
