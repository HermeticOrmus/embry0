"""The dev-mode auth matrix shared by test_dev_mode_auth and test_dev_mode_startup (#518).

AUTH_DEV_MODE x WEBHOOK_DEV_MODE x API_KEY (empty | set) x SSO (off | on).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from embry0.config import Embry0Config

KEY = "master-key-for-the-matrix"
SESSION_SECRET = "s" * 40
# A secret value the startup refusals must never echo.
CLIENT_SECRET = "client-secret-never-logged"
# Not the insecure default password, so the lifespan's Postgres check stays quiet.
DATABASE_URL = "postgresql://user:not-the-default@localhost:5432/db"

SSO_FIELDS = dict(
    auth0_issuer="https://tenant.example.com/",
    auth0_client_id="client-id",
    auth0_client_secret=CLIENT_SECRET,
    session_secret=SESSION_SECRET,
    sso_public_origin="https://embry0.example.com",
    sso_roles_claim="https://example.com/roles",
)


def matrix_config(
    *, auth_dev: bool = False, webhook_dev: bool = False, key: str = KEY, sso: bool = False
) -> Embry0Config:
    """A config for one cell of the matrix, read from nothing but its arguments."""
    with patch.dict(os.environ, {}, clear=True):
        return Embry0Config(
            _env_file=None,
            database_url=DATABASE_URL,
            auth_dev_mode=auth_dev,
            webhook_dev_mode=webhook_dev,
            api_key=key,
            **(SSO_FIELDS if sso else {}),
        )


def combo_id(auth_dev: bool, webhook_dev: bool, key: str, sso: bool) -> str:
    return f"auth_dev={int(auth_dev)}-webhook_dev={int(webhook_dev)}-key={'set' if key else 'empty'}-sso={int(sso)}"


#: Every cell, as (auth_dev, webhook_dev, key, sso).
MATRIX = [
    pytest.param(a, w, k, s, id=combo_id(a, w, k, s))
    for a in (False, True)
    for w in (False, True)
    for k in ("", KEY)
    for s in (False, True)
]
