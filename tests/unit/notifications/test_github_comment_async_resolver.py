from unittest.mock import MagicMock

from embry0.notifications.github_comment import GitHubCommentChannel


async def test_async_token_resolver_is_awaited():
    async def resolver(repo: str) -> str:
        return "tok-" + repo

    channel = GitHubCommentChannel(
        http_client=MagicMock(), dashboard_base_url="", token_resolver=resolver, inputs_repo=MagicMock()
    )
    assert await channel._token_for("acme/widgets") == "tok-acme/widgets"


async def test_sync_token_resolver_still_works():
    channel = GitHubCommentChannel(
        http_client=MagicMock(), dashboard_base_url="", token_resolver=lambda r: "sync-" + r, inputs_repo=MagicMock()
    )
    assert await channel._token_for("acme/widgets") == "sync-acme/widgets"


async def test_no_token_resolver_yields_empty_token():
    channel = GitHubCommentChannel(http_client=MagicMock(), dashboard_base_url="")
    assert await channel._token_for("acme/widgets") == ""
