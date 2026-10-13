"""#294: nothing the watcher sends to the model or files in a tracker is raw.

One fixture log carrying every secret kind embry0 knows, plus an e-mail,
driven through a whole tick: the analysis task (the briefing and the excerpts)
and the draft filed from the model's proposal must carry none of them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from tests.fixtures.redaction_corpus import _GH, CORPUS, EMAIL
from tests.unit.services.test_watcher import _config, _service

_LINES = [f"[api] ERROR {line}" for line, _ in CORPUS]
_SECRETS = [secret for _, secret in CORPUS]


def _proposal_quoting_the_logs() -> str:
    quoted = "\n".join(f"    {line}" for line in _LINES)
    return (
        f"TITLE: Mailbox {EMAIL} rejected with {_GH}\n"
        "SEVERITY: high\n"
        f"RATIONALE: {EMAIL} keeps failing\n"
        "BODY:\n"
        f"## Evidence\n{quoted}\n## Impact\nmail stops"
    )


def _leaks(text: str) -> list[str]:
    return [secret for secret in _SECRETS if secret in text]


@pytest.mark.parametrize("clustering", ["off", "on"])
async def test_the_analysis_task_carries_no_secret_and_no_email(clustering: str) -> None:
    job = {"job_id": "job-w1", "status": "completed", "result_summary": "NO_ISSUE"}
    svc = _service(config=_config(log_clustering_mode=clustering), lines=_LINES, job_result=job)
    await svc.run_once()
    task = svc._jobs.create.await_args.kwargs["task"]
    prompt = svc._executor.issues[0]["body"]
    assert "Log excerpts:" in task, "the tick never reached the analysis step"
    assert _leaks(task) == [], f"the analysis task (stored on the job row) leaks {_leaks(task)}"
    assert _leaks(prompt) == [], f"the prompt the agent reads leaks {_leaks(prompt)}"
    assert "<email>" in task


async def test_briefing_sections_are_redacted_before_they_are_cut() -> None:
    """A briefing item is cut to a length. Cutting first would leave half a
    token that no rule recognises — the front of a ghp_ token is still a
    credential fragment."""
    straddling = "x " * 95 + _GH  # the 200-char cut lands inside the token
    svc = _service(
        lines=["[c] e1", "[c] e2", "[c] e3"],
        job_result={"job_id": "job-w1", "status": "completed", "result_summary": "NO_ISSUE"},
        open_issues=[{"identifier": "ENG-42", "title": f"Mail to {EMAIL} bounces", "state": "Todo"}],
        findings=[
            {
                "action": "no_issue",
                "detail": None,
                "fingerprint": "fp",
                "rationale": straddling,
                "created_at": datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
            }
        ],
        patterns=[{"fingerprint": "p", "summary": f"benign for {EMAIL}", "occurrences": 2}],
    )
    svc._fetch_deploy_markers = AsyncMock(return_value=[f"deploy 2026-07-30 12:58 UTC [ci] by {EMAIL}"])
    await svc.run_once()
    task = svc._jobs.create.await_args.kwargs["task"]
    assert EMAIL not in task, "an e-mail in a briefing section reached the prompt"
    assert straddling[190:200] not in task, (
        "a token cut at the briefing's length limit left its front half in the prompt"
    )


async def test_the_filed_draft_carries_no_secret_and_no_email() -> None:
    job = {"job_id": "job-w1", "status": "completed", "result_summary": _proposal_quoting_the_logs()}
    issue = {"identifier": "ENG-2001", "url": "https://tracker.example/ENG-2001"}
    svc = _service(lines=_LINES, job_result=job, linear_issue=issue)
    out = await svc.run_once()
    assert out["action"] == "proposed", out
    filed = svc._filer.file_draft.await_args.kwargs
    assert _leaks(filed["description"]) == [], f"the filed ticket body leaks {_leaks(filed['description'])}"
    assert _leaks(filed["title"]) == [] and EMAIL not in filed["title"], f"the filed title leaks: {filed['title']}"
    assert "<email>" in filed["description"]
    # The ledger and the run row get the same redacted text, not the raw one.
    recorded = svc._runs.create.await_args.kwargs
    assert EMAIL not in str(recorded.get("detail")) and EMAIL not in str(recorded.get("rationale"))


def test_cluster_samples_are_redacted_before_they_are_trimmed() -> None:
    from embry0.safety.redaction import redact_log_text
    from embry0.services.log_signatures import LogEntry, cluster_entries, format_cluster_table

    line = "y " * 105 + _GH  # the 220-char sample trim lands inside the token
    table = format_cluster_table(cluster_entries([LogEntry(line=line)]), redact=redact_log_text)
    assert line[210:220] not in table, "a sample trimmed before redaction left a token's front half"
