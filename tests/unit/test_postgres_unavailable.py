"""#509: an unreachable test database fails a run that asked for one."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_without_test_database_url_it_skips(monkeypatch):
    from tests.fixtures.postgres import postgres_unavailable

    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(pytest.skip.Exception) as exc_info:
        postgres_unavailable(ConnectionRefusedError("refused"))
    assert "PostgreSQL unavailable" in str(exc_info.value), str(exc_info.value)


def test_with_test_database_url_it_fails(monkeypatch):
    from tests.fixtures.postgres import postgres_unavailable

    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
    with pytest.raises(pytest.fail.Exception) as exc_info:
        postgres_unavailable(ConnectionRefusedError("refused"))
    assert "TEST_DATABASE_URL is set" in str(exc_info.value), (
        f"an explicit test database that is unreachable must fail the test, not skip it: {exc_info.value}"
    )


_OLD_SKIP = 'pytest.skip(f"PostgreSQL ' + "unavailable"


def test_every_db_fixture_uses_the_shared_helper():
    """No copy of the old skip survives: it would turn a dead database into silent skips."""
    root = Path(__file__).resolve().parents[2]
    stale = [
        str(p.relative_to(root))
        for p in (root / "tests").rglob("*.py")
        if _OLD_SKIP in p.read_text(encoding="utf-8")
        and p not in {Path(__file__).resolve(), root / "tests" / "fixtures" / "postgres.py"}
    ]
    assert not stale, f"these files still skip on an unreachable database: {stale}"
