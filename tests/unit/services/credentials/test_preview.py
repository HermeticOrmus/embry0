from embry0.services.credentials.preview import masked_preview


def test_short_secret_fully_masked() -> None:
    assert masked_preview("ghp_short") == "••••••••"


def test_long_secret_shows_edges() -> None:
    assert masked_preview("ghp_abcdefghijklmnop") == "ghp_••••••••mnop"
