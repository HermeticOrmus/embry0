"""LinearSyncProvider — credentials resolve per delivery, no restart (#157)."""

from embry0.services.linear_provider import LinearSyncProvider


class FakeIntegrationRepo:
    def __init__(self, api_key="", secret="", repo_map="") -> None:
        self.creds = (api_key, secret, repo_map)
        self.calls = 0

    async def get_linear_credentials(self):
        self.calls += 1
        return self.creds


async def test_returns_none_when_no_api_key_configured():
    repo = FakeIntegrationRepo(api_key="", secret="whsec")
    service, secret = await LinearSyncProvider(repo).resolve()
    assert service is None
    # The secret still comes back: signature verification runs before we know
    # whether the integration is usable.
    assert secret == "whsec"


async def test_builds_a_service_from_credentials():
    repo = FakeIntegrationRepo(api_key="lin_key", secret="whsec", repo_map='{"Proj": "o/r"}')
    service, secret = await LinearSyncProvider(repo).resolve()
    assert service is not None
    assert secret == "whsec"
    assert service._repo_map == {"Proj": "o/r"}


async def test_reuses_the_service_while_credentials_are_unchanged():
    provider = LinearSyncProvider(FakeIntegrationRepo(api_key="lin_key"))
    first, _ = await provider.resolve()
    second, _ = await provider.resolve()
    assert first is second


async def test_rebuilds_when_the_api_key_changes():
    """Editing the key in Settings must take effect without a restart."""
    repo = FakeIntegrationRepo(api_key="old")
    provider = LinearSyncProvider(repo)
    first, _ = await provider.resolve()

    repo.creds = ("new", "", "")
    second, _ = await provider.resolve()

    assert first is not second
    assert second._api_key == "new"


async def test_rebuilds_when_only_the_repo_map_changes():
    repo = FakeIntegrationRepo(api_key="k", repo_map='{"A": "o/a"}')
    provider = LinearSyncProvider(repo)
    first, _ = await provider.resolve()

    repo.creds = ("k", "", '{"B": "o/b"}')
    second, _ = await provider.resolve()

    assert first is not second
    assert second._repo_map == {"B": "o/b"}


async def test_clearing_the_key_drops_the_cached_service():
    repo = FakeIntegrationRepo(api_key="k")
    provider = LinearSyncProvider(repo)
    assert (await provider.resolve())[0] is not None

    repo.creds = ("", "", "")
    service, _ = await provider.resolve()
    assert service is None

    repo.creds = ("k", "", "")
    assert (await provider.resolve())[0] is not None
