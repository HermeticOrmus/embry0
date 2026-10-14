"""A watcher case is anchored to its own error, not to the window's loudest line (#657).

#290 keyed every case on ``WindowDelta.primary`` — the window's loudest notable
cluster — whatever the proposal was about. A proposal about a quieter error
was filed under an unrelated line: confirm-healed then judged that line, and
dedup keyed on it, so one root cause opened several cases while two different
faults in one window collapsed onto one.

These run a real ``WatcherService`` on real repositories, so they cover the
whole path: cluster the window, read the proposal's evidence, anchor the case.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from embry0.services.log_signatures import normalize_line, signature_id
from tests.unit.services.healing.watcher_harness import _as_entries, build_watcher

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

REPO = "owner/repo"

# The window's loudest line: an edge proxy that cannot resolve its origin.
LOUD = 'ERR Request failed error="dial tcp: lookup web on resolver: no such host" connIndex=0 dest=https://app.test/'
# Louder still in a later window, and unrelated to everything else.
LOUDER = "WARN cache refresh skipped: upstream returned 429 Too Many Requests, retrying in 30s"
# The error the proposal is about: rarer, so never the loudest.
QUIET = "2026-09-20 12:00:05.123 UTC [4242] ERROR:  column d.thread_ref does not exist at character 66"
# A second, unrelated fault in the same window.
OTHER = "WARN WebhookController : payment webhook rejected: signature header missing for provider sandbox"


def _sig(line: str) -> str:
    return signature_id(normalize_line(line))


def _window(*parts: tuple[str, int]) -> AsyncMock:
    lines = [line for line, count in parts for _ in range(count)]
    return AsyncMock(return_value=_as_entries(lines))


def _proposal(title: str, evidence: str) -> str:
    return (
        f"TITLE: {title}\nSEVERITY: medium\nRATIONALE: deterministic error, not covered by any ticket\n"
        f"BODY:\n## Evidence\n{evidence}\n\n## Impact\nEvery run of the job fails.\n\n"
        "## Suspected cause\nA recent change.\n\n## Suggested next step\nFix it and add a test."
    )


ABOUT_QUIET = _proposal(
    "Fix the broken column alias in the export job",
    f"- `[svc] {QUIET}` — 3 occurrences in the window, starting right after the deploy.",
)
ABOUT_QUIET_REWORDED = _proposal(
    "Export job query references a column that does not exist",
    "The scheduled export fails on every run (postgres, 3x):\n```\n" + QUIET + "\n```",
)
ABOUT_OTHER = _proposal(
    "Payment webhooks rejected for a missing signature header",
    f"- [svc] {OTHER} (2x)",
)
ABOUT_NOTHING_IN_THE_WINDOW = _proposal(
    "Breaker keeps opening on the scoring service",
    "Found by drill-down, not in the excerpt:\n```\nscoring breaker OPEN for 900s after 12 consecutive failures\n```",
)


async def _cases(db) -> list[dict[str, Any]]:
    rows = await db.fetch("SELECT id, signature_id, signature, signature_source FROM healing_cases ORDER BY created_at")
    return [dict(r) for r in rows]


async def test_the_case_is_anchored_to_the_error_its_evidence_quotes(db_with_migrations) -> None:
    """The headline of #657: the window's loudest line is NOT the proposal's error."""
    svc, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=[ABOUT_QUIET])
    svc._fetch_log_entries = _window((LOUD, 8), (QUIET, 3))  # type: ignore[method-assign]

    out = await svc.run_once()
    assert out["action"] == "proposed", out

    row = await db_with_migrations.fetchrow("SELECT signature_id, signature FROM healing_cases")
    assert row is not None, "no case was opened"
    assert row["signature_id"] == _sig(QUIET), (
        f"the case was anchored to {row['signature']!r} — the window's loudest line — "
        f"instead of the error its evidence quotes ({normalize_line(QUIET)!r})"
    )
    assert row["signature"] == normalize_line(QUIET)


async def test_two_faults_in_one_window_get_two_cases(db_with_migrations) -> None:
    """Anchored to the loudest line, the second finding read as a duplicate of the first."""
    svc, linear, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=[ABOUT_QUIET, ABOUT_OTHER])
    svc._fetch_log_entries = _window((LOUD, 8), (QUIET, 3), (OTHER, 2))  # type: ignore[method-assign]

    assert (await svc.run_once())["action"] == "proposed"
    second = await svc.run_once()
    assert second["action"] == "proposed", (
        f"a different fault in the same window was swallowed as a duplicate of the first: {second}"
    )
    assert [c["signature_id"] for c in await _cases(db_with_migrations)] == [_sig(QUIET), _sig(OTHER)]
    assert len(linear.created) == 2


async def test_a_reworded_redetection_dedupes_even_when_the_loudest_line_changed(db_with_migrations) -> None:
    """Dedup follows the error, not whatever happened to be loudest that hour."""
    svc, linear, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=[ABOUT_QUIET, ABOUT_QUIET_REWORDED])
    svc._fetch_log_entries = _window((LOUD, 8), (QUIET, 3))  # type: ignore[method-assign]
    assert (await svc.run_once())["action"] == "proposed"

    svc._fetch_log_entries = _window((LOUDER, 20), (QUIET, 3))  # type: ignore[method-assign]
    second = await svc.run_once()
    assert second["action"] == "skipped_duplicate", (
        f"the same error, re-detected under a louder unrelated line, opened a second case: {second}"
    )
    cases = await _cases(db_with_migrations)
    assert len(cases) == 1 and len(linear.created) == 1


async def test_the_case_records_that_its_signature_came_from_the_evidence(db_with_migrations) -> None:
    svc, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=[ABOUT_QUIET])
    svc._fetch_log_entries = _window((LOUD, 8), (QUIET, 3))  # type: ignore[method-assign]
    await svc.run_once()

    [case] = await _cases(db_with_migrations)
    assert case["signature_source"] == "evidence"


async def test_nothing_quoted_in_the_window_falls_back_to_the_loudest_line_and_says_so(db_with_migrations) -> None:
    svc, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=[ABOUT_NOTHING_IN_THE_WINDOW])
    svc._fetch_log_entries = _window((LOUD, 8), (QUIET, 3))  # type: ignore[method-assign]

    assert (await svc.run_once())["action"] == "proposed"
    [case] = await _cases(db_with_migrations)
    assert case["signature_id"] == _sig(LOUD), "the fallback is #290's rule: the window's loudest line"
    assert case["signature_source"] == "fallback", (
        "a case anchored to a line its evidence never quotes must say so, or confirm-healed trusts it"
    )


async def test_a_deferred_finding_records_its_source_too(db_with_migrations) -> None:
    """The cap-full path opens its case through a second call; it must pass the source as well."""
    svc, linear, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=[ABOUT_QUIET], max_open=0)
    svc._fetch_log_entries = _window((LOUD, 8), (QUIET, 3))  # type: ignore[method-assign]

    assert (await svc.run_once())["action"] == "deferred"
    assert not linear.created
    [case] = await _cases(db_with_migrations)
    assert (case["signature_id"], case["signature_source"]) == (_sig(QUIET), "evidence")


async def test_a_failure_reading_the_evidence_never_costs_the_finding(db_with_migrations, monkeypatch) -> None:
    import embry0.services.watcher as watcher_module

    def _broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("attribution exploded")

    monkeypatch.setattr(watcher_module, "anchor_signature", _broken)
    svc, _, _ = build_watcher(db_with_migrations, repo=REPO, deliverables=[ABOUT_QUIET])
    svc._fetch_log_entries = _window((LOUD, 8), (QUIET, 3))  # type: ignore[method-assign]

    assert (await svc.run_once())["action"] == "proposed", "a broken attribution cost the tick its finding"
    [case] = await _cases(db_with_migrations)
    assert (case["signature_id"], case["signature_source"]) == (_sig(LOUD), "fallback")
