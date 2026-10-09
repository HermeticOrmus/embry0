"""Row-reading helpers on the normalized tracker columns."""

from embry0.trackers import actor_of, forge_number_of, github_number_of


def test_forge_number_of_reads_github_and_gitlab_keys():
    assert forge_number_of({"tracker": "github", "tracker_key": "42"}) == 42
    assert forge_number_of({"tracker": "gitlab", "tracker_key": "22"}) == 22


def test_forge_number_of_is_none_for_linear_and_untracked():
    """Linear keys (ENG-123) are not forge numbers — a Closes #N built from
    one would reference an unrelated issue on the repo's forge (#213)."""
    assert forge_number_of({"tracker": "linear", "tracker_key": "ENG-9"}) is None
    assert forge_number_of({"tracker": None, "tracker_key": None}) is None
    assert forge_number_of(None) is None
    assert forge_number_of({"tracker": "gitlab", "tracker_key": ""}) is None


def test_github_number_of_stays_github_only():
    assert github_number_of({"tracker": "gitlab", "tracker_key": "22"}) is None


# --- actor_of (#246) --------------------------------------------------------


def test_actor_of_reads_github_sender_login():
    assert actor_of({"sender": {"login": "octocat"}}, "github") == "octocat"


def test_actor_of_reads_gitlab_username():
    assert actor_of({"user": {"username": "gl-user"}}, "gitlab") == "gl-user"


def test_actor_of_prefers_linear_email_over_display_name():
    assert actor_of({"actor": {"email": "t@x.io", "name": "Alex"}}, "linear") == "t@x.io"


def test_actor_of_falls_back_to_the_linear_display_name():
    assert actor_of({"actor": {"name": "Alex"}}, "linear") == "Alex"


def test_actor_of_is_empty_when_the_payload_names_nobody():
    assert actor_of({}, "github") == ""
    assert actor_of({"sender": None}, "github") == ""
    assert actor_of(None, "github") == ""
    assert actor_of({"sender": {"login": "x"}}, "unknown-forge") == ""
