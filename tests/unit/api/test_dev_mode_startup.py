"""Startup refuses the dev-mode states that would expose the API (#518).

Without AUTH_DEV_MODE an empty API_KEY is refused, WEBHOOK_DEV_MODE included:
that flag skips webhook signature checks and nothing else. Either dev flag on
a deployment with a public origin (SSO, or a tunnel token in the process
environment) is refused too. Each cell of the matrix runs the real lifespan up
to its first database touch.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI

from embry0.config import Embry0Config
from tests.unit.api.dev_mode_matrix import CLIENT_SECRET, KEY, combo_id, matrix_config

# A secret value the startup refusals must never echo.
TUNNEL_SECRET = "tunnel-token-never-logged"

# (AUTH_DEV_MODE, WEBHOOK_DEV_MODE, API_KEY, SSO, boots). Written out rather
# than derived, so the table is the specification and not a copy of the code.
STARTUP_TABLE = [
    (False, False, "", False, False),  # no key, no dev flag
    (False, False, "", True, False),
    (False, False, KEY, False, True),  # the plain LAN deployment
    (False, False, KEY, True, True),  # SSO deployment, no dev flag
    (False, True, "", False, False),  # #518: the smee recipe without a key
    (False, True, "", True, False),
    (False, True, KEY, False, True),  # the smee recipe with a key
    (False, True, KEY, True, False),  # webhook dev mode on a public origin
    (True, False, "", False, True),  # a local unauthenticated instance
    (True, False, "", True, False),  # auth off on a public origin
    (True, False, KEY, False, True),
    (True, False, KEY, True, False),
    (True, True, "", False, True),
    (True, True, "", True, False),
    (True, True, KEY, False, True),
    (True, True, KEY, True, False),
]


class _ReachedDatabase(Exception):
    """Raised in place of the DB pool: the lifespan passed every startup refusal."""


async def _startup_refusal(config: Embry0Config, environ: dict[str, str] | None = None) -> str | None:
    """Run the real lifespan until its first database touch.

    Returns the refusal message, or None when startup got as far as the
    database. ``environ`` replaces the process environment for the run.
    """
    from embry0.api.app import lifespan

    app = FastAPI()
    app.state.config = config
    with (
        patch.dict(os.environ, environ or {}, clear=True),
        patch("embry0.observability.configure_structlog"),
        patch("embry0.api.app.DatabasePool", side_effect=_ReachedDatabase),
    ):
        try:
            async with lifespan(app):
                pass
        except _ReachedDatabase:
            return None
        except RuntimeError as exc:
            return str(exc)
    pytest.fail("the lifespan neither refused nor reached the database")


@pytest.mark.parametrize(
    ("auth_dev", "webhook_dev", "key", "sso", "boots"),
    [pytest.param(*row, id=combo_id(*row[:4])) for row in STARTUP_TABLE],
)
async def test_startup_outcome(auth_dev: bool, webhook_dev: bool, key: str, sso: bool, boots: bool) -> None:
    refusal = await _startup_refusal(matrix_config(auth_dev=auth_dev, webhook_dev=webhook_dev, key=key, sso=sso))
    outcome = "boots" if refusal is None else f"refuses: {refusal}"
    assert (refusal is None) == boots, f"expected {'boot' if boots else 'a refusal'}, got: {outcome}"


async def test_webhook_dev_mode_without_a_key_names_the_way_out() -> None:
    refusal = await _startup_refusal(matrix_config(webhook_dev=True, key=""))
    assert refusal is not None, "WEBHOOK_DEV_MODE with an empty API_KEY booted with API auth off"
    assert "API_KEY" in refusal and "AUTH_DEV_MODE" in refusal, refusal


@pytest.mark.parametrize(("flag", "env_name"), [("auth_dev", "AUTH_DEV_MODE"), ("webhook_dev", "WEBHOOK_DEV_MODE")])
async def test_public_origin_refusal_names_settings_but_never_values(flag: str, env_name: str) -> None:
    refusal = await _startup_refusal(matrix_config(sso=True, **{flag: True}))
    assert refusal is not None, f"{env_name} booted on an SSO deployment"
    assert env_name in refusal and "SSO_PUBLIC_ORIGIN" in refusal, refusal
    assert CLIENT_SECRET not in refusal, "the refusal echoed the client secret"


@pytest.mark.parametrize("env_name", ["CLOUDFLARED_TUNNEL_TOKEN", "TUNNEL_TOKEN"])
@pytest.mark.parametrize("flag", ["auth_dev", "webhook_dev"])
async def test_dev_flag_with_a_tunnel_token_refuses_startup(flag: str, env_name: str) -> None:
    refusal = await _startup_refusal(matrix_config(**{flag: True}), {env_name: TUNNEL_SECRET})
    assert refusal is not None, f"{flag} booted with {env_name} set"
    assert env_name in refusal, refusal
    assert TUNNEL_SECRET not in refusal, "the refusal echoed the tunnel token"


async def test_blank_tunnel_token_is_not_a_public_origin() -> None:
    refusal = await _startup_refusal(
        matrix_config(webhook_dev=True), {"CLOUDFLARED_TUNNEL_TOKEN": "", "TUNNEL_TOKEN": "  "}
    )
    assert refusal is None, f"blank tunnel tokens refused a local dev instance: {refusal}"


async def test_a_deployment_without_dev_flags_boots_with_sso_and_a_tunnel() -> None:
    """The production shape: dev flags off, API_KEY set, SSO on, a tunnel."""
    refusal = await _startup_refusal(
        matrix_config(sso=True), {"CLOUDFLARED_TUNNEL_TOKEN": TUNNEL_SECRET, "TUNNEL_TOKEN": TUNNEL_SECRET}
    )
    assert refusal is None, f"a deployment with no dev flag was refused: {refusal}"
