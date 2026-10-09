"""DEFAULT_FORGE="" makes an unregistered repo fail loud (Spec 1A §3.8)."""

import pytest

from embry0.safety.error_codes import ErrorCode, error_code_for_exception
from embry0.storage.repositories.repos import (
    DEFAULT_FORGE,
    ReposRepository,
    UnregisteredRepoError,
)


class FakeDB:
    """Minimal DatabasePool stand-in: one row table keyed by repo."""

    def __init__(self, rows: dict[str, str] | None = None) -> None:
        self._rows = rows or {}

    async def fetchrow(self, _sql: str, repo: str):
        forge = self._rows.get(repo)
        return {"forge": forge} if forge else None


async def test_registered_repo_returns_its_forge_regardless_of_default():
    repo = ReposRepository(FakeDB({"acme/products/fbr": "gitlab"}), default_forge="")
    assert await repo.forge_for("acme/products/fbr") == "gitlab"


async def test_unregistered_repo_uses_the_default_forge_by_default():
    """Pre-#162 deployments meant github; the default must not change."""
    repo = ReposRepository(FakeDB())
    assert await repo.forge_for("owner/name") == DEFAULT_FORGE == "github"


async def test_unregistered_repo_raises_when_default_forge_is_empty():
    repo = ReposRepository(FakeDB(), default_forge="")
    with pytest.raises(UnregisteredRepoError) as exc:
        await repo.forge_for("acme/group/backend")
    # The message must tell the operator what to do, not just what failed.
    assert "acme/group/backend" in str(exc.value)
    assert "POST /repos" in str(exc.value)


async def test_unregistered_repo_error_is_a_lookup_error():
    """Callers that already catch LookupError keep working."""
    assert issubclass(UnregisteredRepoError, LookupError)


async def test_unregistered_repo_error_classifies_to_its_own_code():
    assert error_code_for_exception(UnregisteredRepoError("x")) is ErrorCode.REPO_UNREGISTERED
    assert ErrorCode.REPO_UNREGISTERED.value == "ERR_REPO_UNREGISTERED"


def test_unsupported_default_forge_is_rejected_at_construction():
    with pytest.raises(ValueError, match="unsupported default forge"):
        ReposRepository(FakeDB(), default_forge="bitbucket")
