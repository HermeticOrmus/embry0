"""Publishing a verification result: deliverables, PR comment, label (#298, #299)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embry0.services.verification_publish import publish_verification, verification_deliverables


def _report(*, human: bool = False, verdict: str = "passed") -> dict[str, Any]:
    run = {"target": "jvm", "attempt": 1, "outcome": "passed", "exit_code": 0, "output_tail": "ok"}
    return {
        "schema_version": 1,
        "mode": "on",
        "tier": {"tier": "frontend" if human else "backend_only"},
        "gate": {
            "verdict": verdict,
            "base_sha": "b" * 40,
            "head_sha": "h" * 40,
            "targets": [],
            "runs": [{**run, "side": "head"}, {**run, "side": "base"}],
        },
        "browser_qa": {"planned": "skipped", "reason_code": "no_branch_stack" if human else "backend_only"},
        "blocking": verdict == "new_failures",
        "human_merge_only": human,
        "human_merge_reasons": ["no_branch_stack"] if human else [],
    }


def _repos(row: dict[str, Any] | None) -> tuple[MagicMock, MagicMock]:
    verifications = MagicMock()
    verifications.get = AsyncMock(return_value=row)
    verifications.mark_published = AsyncMock()
    deliverables = MagicMock()
    deliverables.create = AsyncMock(return_value="del-1")
    return verifications, deliverables


def _row(report: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"job_id": "job-1", "mode": "on", "published_at": None, "report": report, **extra}


def test_base_and_head_runs_and_the_verdict_become_three_deliverables() -> None:
    rows = verification_deliverables(_report())
    assert [r["metadata"]["kind"] for r in rows] == ["verification_run", "verification_run", "verification_verdict"]
    assert [r["metadata"].get("side") for r in rows[:2]] == ["base", "head"]
    assert all(r["type"] == "report" and r["media_type"] == "application/json" for r in rows)
    assert json.loads(rows[0]["content"])["sha"] == "b" * 40
    verdict = json.loads(rows[2]["content"])
    assert verdict["gate"]["verdict"] == "passed" and "runs" not in verdict["gate"]


def test_a_result_without_runs_still_yields_the_verdict() -> None:
    report = _report(verdict="not_configured")
    report["gate"]["runs"] = []
    assert [r["metadata"]["kind"] for r in verification_deliverables(report)] == ["verification_verdict"]


@pytest.mark.asyncio
async def test_github_pr_gets_a_sticky_comment_and_no_label_when_machine_mergeable() -> None:
    verifications, deliverables = _repos(_row(_report()))
    client = MagicMock()
    client.post = AsyncMock()
    with (
        patch("embry0.notifications.qa_comment.upsert_sticky_comment", new=AsyncMock(return_value=1)) as upsert,
        patch("embry0.execution.github_client.GitHubClient", return_value=client),
    ):
        published = await publish_verification(
            job_id="job-1",
            pr_url="https://github.com/acme/backend/pull/530",
            verifications_repo=verifications,
            deliverables_repo=deliverables,
            github_token="t",
        )
    assert published is True
    assert upsert.await_args.kwargs["repo"] == "acme/backend"
    assert upsert.await_args.kwargs["issue_number"] == 530
    assert upsert.await_args.kwargs["marker"] == "<!-- embry0:verification -->"
    client.post.assert_not_awaited()
    assert deliverables.create.await_count == 3
    verifications.mark_published.assert_awaited_once_with("job-1")


@pytest.mark.asyncio
async def test_human_merge_only_adds_the_label() -> None:
    verifications, deliverables = _repos(_row(_report(human=True)))
    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))
    with (
        patch("embry0.notifications.qa_comment.upsert_sticky_comment", new=AsyncMock(return_value=1)),
        patch("embry0.execution.github_client.GitHubClient", return_value=client),
    ):
        await publish_verification(
            job_id="job-1",
            pr_url="https://github.com/o/r/pull/7",
            verifications_repo=verifications,
            deliverables_repo=deliverables,
        )
    assert client.post.await_args.args[0] == "/repos/o/r/issues/7/labels"
    assert client.post.await_args.kwargs["json"] == {"labels": ["human-merge-only"]}


@pytest.mark.asyncio
async def test_gitlab_mr_gets_a_note_and_the_label() -> None:
    verifications, deliverables = _repos(_row(_report(human=True)))
    client = MagicMock()
    client.create_mr_note = AsyncMock(return_value=True)
    client.add_mr_labels = AsyncMock(return_value=True)
    with patch("embry0.execution.gitlab_client.make_gitlab_client", return_value=client):
        await publish_verification(
            job_id="job-1",
            pr_url="https://gitlab.example.com/acme/products/fbr/-/merge_requests/12",
            verifications_repo=verifications,
            deliverables_repo=deliverables,
        )
    assert client.create_mr_note.await_args.args[:2] == ("acme/products/fbr", 12)
    client.add_mr_labels.assert_awaited_once_with("acme/products/fbr", 12, ["human-merge-only"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [None, _row(_report(), published_at="2026-09-20T00:00:00Z"), _row(_report(), mode="shadow")],
    ids=["no-result", "already-published", "shadow"],
)
async def test_nothing_is_published_twice_or_in_shadow(row: dict[str, Any] | None) -> None:
    verifications, deliverables = _repos(row)
    assert (
        await publish_verification(
            job_id="job-1",
            pr_url="https://github.com/o/r/pull/7",
            verifications_repo=verifications,
            deliverables_repo=deliverables,
        )
        is False
    )
    deliverables.create.assert_not_awaited()
    verifications.mark_published.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_comment_still_stores_deliverables_and_never_raises() -> None:
    verifications, deliverables = _repos(_row(_report()))
    with patch("embry0.notifications.qa_comment.upsert_sticky_comment", new=AsyncMock(side_effect=RuntimeError("403"))):
        assert await publish_verification(
            job_id="job-1",
            pr_url="https://github.com/o/r/pull/7",
            verifications_repo=verifications,
            deliverables_repo=deliverables,
        )
    assert deliverables.create.await_count == 3
    verifications.mark_published.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_pull_request_means_deliverables_only() -> None:
    verifications, deliverables = _repos(_row(_report()))
    assert await publish_verification(
        job_id="job-1", pr_url=None, verifications_repo=verifications, deliverables_repo=deliverables
    )
    assert deliverables.create.await_count == 3


@pytest.mark.asyncio
async def test_github_comment_client_carries_the_credential_resolver() -> None:
    verifications, deliverables = _repos(_row(_report()))
    resolver = MagicMock()
    with patch("embry0.notifications.qa_comment.upsert_sticky_comment", new=AsyncMock(return_value=1)) as upsert:
        await publish_verification(
            job_id="job-1",
            pr_url="https://github.com/acme/widgets/pull/7",
            verifications_repo=verifications,
            deliverables_repo=deliverables,
            github_token="t",
            credential_resolver=resolver,
        )
    assert upsert.await_args.kwargs["client"]._credential_resolver is resolver


@pytest.mark.asyncio
async def test_gitlab_note_client_carries_the_credential_resolver() -> None:
    verifications, deliverables = _repos(_row(_report()))
    resolver = MagicMock()
    client = MagicMock()
    client.create_mr_note = AsyncMock(return_value=True)
    with patch("embry0.execution.gitlab_client.make_gitlab_client", return_value=client) as factory:
        await publish_verification(
            job_id="job-1",
            pr_url="https://gitlab.example.com/group/proj/-/merge_requests/3",
            verifications_repo=verifications,
            deliverables_repo=deliverables,
            config=MagicMock(),
            credential_resolver=resolver,
        )
    assert factory.call_args.kwargs["credential_resolver"] is resolver


@pytest.mark.asyncio
async def test_the_executor_hands_its_resolver_to_publish_verification() -> None:
    from embry0.services._executor_outcomes import _publish_verification
    from embry0.services.issue_executor import IssueExecutor

    ex = IssueExecutor.__new__(IssueExecutor)
    ex._db = MagicMock()
    ex._credential_resolver = MagicMock()
    with patch("embry0.services.verification_publish.publish_verification", new=AsyncMock()) as publish:
        await _publish_verification(ex, "job-1", "https://github.com/acme/widgets/pull/7")
    assert publish.await_args.kwargs["credential_resolver"] is ex._credential_resolver
