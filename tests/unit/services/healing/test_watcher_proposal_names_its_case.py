"""The `watcher.proposed` notification carries the case id (#378).

That id is the address an operator — or the chat bot relaying one — hands
to ``POST /cases/{id}/accept`` when the draft's tracker has no label path.
Real ledger, so the id in the payload is the row that was actually opened.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.unit.services.healing.watcher_harness import build_watcher, proposal

REPO = "acme/backend"


@pytest.mark.asyncio
async def test_the_proposal_event_names_the_case_it_opened(db_with_migrations) -> None:
    svc, _linear, _jobs = build_watcher(db_with_migrations, repo=REPO, deliverables=[proposal("Fix X")])
    notifier = MagicMock()
    svc._webhook_notifier = notifier
    out = await svc.run_once()
    assert out["action"] == "proposed", out

    event, data = notifier.emit_soon.call_args.args
    assert event == "watcher.proposed"
    row = await db_with_migrations.fetchrow(
        "SELECT id, stage FROM healing_cases WHERE tracker = 'linear' AND tracker_key = $1", data["identifier"]
    )
    assert row is not None, "the proposal opened no case"
    assert data["case_id"] == row["id"], f"the event names {data['case_id']!r}, the ledger opened {row['id']!r}"
    assert row["stage"] == "tracked"
    assert data["tracker"] == "linear" and data["repo"] == REPO
