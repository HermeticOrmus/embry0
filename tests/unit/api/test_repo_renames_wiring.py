"""REPO_RENAMES reaches the code that normalises through it (#527).

The renames are read off the environment at startup and installed for the
whole process. Merge sync and the healing ledger never take the map as an
argument, so a startup that forgot to install it would leave every caller on
an empty map and look exactly like "no rename configured" (#369).
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from embry0.config import Embry0Config
from embry0.storage import repo_renames
from embry0.storage.repo_renames import normalize_repo_url

RAW = '{"acme/old": "acme/new"}'
OLD = "https://github.com/acme/old/pull/1"
NEW = "https://github.com/acme/new/pull/1"


@pytest.fixture(autouse=True)
def _restore_installed_map(monkeypatch):
    monkeypatch.setattr(repo_renames, "REPO_RENAMES", {})


def _start(config: Embry0Config) -> None:
    from embry0.api.app import _init_app_state, create_app

    app = create_app(config)
    app.state.secrets_provider = MagicMock()
    asyncio.run(_init_app_state(app=app, db=MagicMock(), database_url="postgresql://test/test", github_token="t" * 40))


def test_the_env_var_is_read_verbatim():
    with patch.dict(os.environ, {"REPO_RENAMES": RAW}, clear=True):
        cfg = Embry0Config(_env_file=None)  # type: ignore[call-arg]
    assert getattr(cfg, "repo_renames", None) == RAW, "REPO_RENAMES is not a config setting"


def test_repo_renames_is_empty_by_default():
    with patch.dict(os.environ, {}, clear=True):
        cfg = Embry0Config(_env_file=None)  # type: ignore[call-arg]
    assert getattr(cfg, "repo_renames", "") == "", "the product must ship no rename"


def test_startup_installs_the_configured_renames():
    with patch.dict(os.environ, {"REPO_RENAMES": RAW}, clear=True):
        _start(Embry0Config(_env_file=None))  # type: ignore[call-arg]
    assert normalize_repo_url(OLD) == NEW, "startup did not install REPO_RENAMES for merge sync and the ledger"


def test_startup_without_renames_renames_nothing():
    repo_renames.REPO_RENAMES = {"acme/old": "acme/new"}  # left over from an earlier start
    with patch.dict(os.environ, {}, clear=True):
        _start(Embry0Config(_env_file=None))  # type: ignore[call-arg]
    assert normalize_repo_url(OLD) == OLD, "an unset REPO_RENAMES must rename nothing"
