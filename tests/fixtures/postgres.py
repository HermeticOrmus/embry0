"""What a database test does when its PostgreSQL cannot be reached (#509).

A bare local run, with ``TEST_DATABASE_URL`` unset, skips: the suite stays
usable on a machine with no Postgres. A run that sets ``TEST_DATABASE_URL``
has said a database is part of it, so an unreachable one FAILS — otherwise a
misconfigured or slow database turns ~800 DB tests into skips nobody reads,
and the SQL layer goes untested while the run looks clean.

Every DB fixture and test that catches a connection error calls this one
helper, so the rule cannot drift between copies.
"""

from __future__ import annotations

import os
from typing import NoReturn

import pytest


def postgres_unavailable(exc: BaseException) -> NoReturn:
    """Skip without TEST_DATABASE_URL; fail with it. Never returns."""
    if os.environ.get("TEST_DATABASE_URL"):
        pytest.fail(
            f"TEST_DATABASE_URL is set but PostgreSQL is unreachable: {exc}",
            pytrace=False,
        )
    pytest.skip(f"PostgreSQL unavailable: {exc}")
