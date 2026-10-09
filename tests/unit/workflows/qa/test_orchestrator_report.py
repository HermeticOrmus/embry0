"""Tests for qa_report_node — sticky QA PR comment writer."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

from embry0.workflows.qa.orchestrator_report import qa_report_node


@pytest.fixture(autouse=True)
def _clean_owner_tokens(monkeypatch):
    for key in [k for k in os.environ if k.startswith("GITHUB_TOKEN__")]:
        monkeypatch.delenv(key)


def _state(**qa_overrides):
    qa = {
        "outcome": {
            "overall_status": "passed",
            "apps_to_qa": ["hub"],
            "failure_summary": None,
            "validation_errors": [],
            "validation_warnings": [],
        },
        "per_app_results": [
            {
                "app_name": "hub",
                "status": "passed",
                "duration_ms": 1000,
                "cache_hits": {
                    "prebaked_image": False,
                    "shared_volume": False,
                    "turbo_remote_hits": [],
                    "turbo_remote_misses": [],
                },
                "trace_url": None,
                "failure_summary": None,
                "raw_result": {},
            }
        ],
        "apps_to_qa": ["hub"],
        "head_sha": "abc123",
        "validation_warnings": [],
    }
    qa.update(qa_overrides)
    return {
        "job_id": "run-1",
        "repo": "org/repo",
        "issue_number": 42,
        "attempt_number": 1,
        "qa": qa,
    }


@pytest.mark.asyncio
async def test_writes_comment_on_happy_path(monkeypatch):
    written_comment = AsyncMock(return_value=99)
    monkeypatch.setattr(
        "embry0.workflows.qa.orchestrator_report.upsert_sticky_comment",
        written_comment,
    )

    config = {"configurable": {"github_token": "tok"}}
    out = await qa_report_node(_state(), config)
    assert out == {}

    written_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_skips_write_when_no_github_token(monkeypatch):
    written_comment = AsyncMock()
    monkeypatch.setattr(
        "embry0.workflows.qa.orchestrator_report.upsert_sticky_comment",
        written_comment,
    )

    config = {"configurable": {}}  # no github_token
    await qa_report_node(_state(), config)
    written_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_still_written_when_no_head_sha(monkeypatch):
    """head_sha is irrelevant to reporting now that the check run is gone."""
    written_comment = AsyncMock(return_value=99)
    monkeypatch.setattr(
        "embry0.workflows.qa.orchestrator_report.upsert_sticky_comment",
        written_comment,
    )

    config = {"configurable": {"github_token": "tok"}}
    out = await qa_report_node(_state(head_sha=None), config)
    assert out == {}

    written_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_skips_comment_when_no_issue_number(monkeypatch):
    written_comment = AsyncMock()
    monkeypatch.setattr(
        "embry0.workflows.qa.orchestrator_report.upsert_sticky_comment",
        written_comment,
    )

    state = _state()
    state["issue_number"] = None
    config = {"configurable": {"github_token": "tok"}}
    await qa_report_node(state, config)

    written_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_failure_does_not_fail_the_node(monkeypatch):
    """A GitHub-side failure logs a warning; the node still returns cleanly."""
    written_comment = AsyncMock(side_effect=RuntimeError("github 503"))
    monkeypatch.setattr(
        "embry0.workflows.qa.orchestrator_report.upsert_sticky_comment",
        written_comment,
    )

    config = {"configurable": {"github_token": "tok"}}
    out = await qa_report_node(_state(), config)

    assert out == {}
    written_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_run_passes_failure_summary_through(monkeypatch):
    written_comment = AsyncMock(return_value=99)
    monkeypatch.setattr(
        "embry0.workflows.qa.orchestrator_report.upsert_sticky_comment",
        written_comment,
    )

    config = {"configurable": {"github_token": "tok"}}
    state = _state(
        outcome={
            "overall_status": "infra_error",
            "apps_to_qa": [],
            "failure_summary": "qa.yaml not loaded",
            "validation_errors": [],
            "validation_warnings": [],
        }
    )
    await qa_report_node(state, config)

    body = written_comment.await_args.kwargs["body"]
    assert "qa.yaml not loaded" in body


@pytest.mark.asyncio
async def test_qa_report_resolves_per_owner_token(monkeypatch):
    written_comment = AsyncMock(return_value=99)
    monkeypatch.setattr(
        "embry0.workflows.qa.orchestrator_report.upsert_sticky_comment",
        written_comment,
    )
    monkeypatch.setenv("GITHUB_TOKEN__ACME_CORP", "tok-rc")

    state = _state()
    state["repo"] = "acme-corp/widgets"
    config = {"configurable": {"github_token": "tok-default"}}
    await qa_report_node(state, config)

    assert written_comment.await_args.kwargs["github_token"] == "tok-rc"


@pytest.mark.asyncio
async def test_sticky_comment_client_sends_the_resolved_token(monkeypatch):
    """The token that gates the write must be the one sent: the client handed
    to upsert_sticky_comment carries the resolver, not a bare env lookup."""
    from types import SimpleNamespace

    written_comment = AsyncMock(return_value=99)
    monkeypatch.setattr("embry0.workflows.qa.orchestrator_report.upsert_sticky_comment", written_comment)
    resolver = AsyncMock()
    resolver.resolve_forge = AsyncMock(return_value=SimpleNamespace(secret="registry-tok"))

    await qa_report_node(_state(), {"configurable": {"github_token": "tok", "credential_resolver": resolver}})

    client = written_comment.await_args.kwargs.get("client")
    assert client is not None, "upsert_sticky_comment built its own env-resolving client"
    assert client._credential_resolver is resolver


@pytest.mark.asyncio
async def test_a_run_without_a_repo_writes_nothing_and_looks_up_a_string(monkeypatch):
    """forge_token takes a str: a missing repo is looked up as "" (resolve_forge treats
    None and "" alike), and the node still skips the write."""
    from types import SimpleNamespace

    written_comment = AsyncMock(return_value=99)
    monkeypatch.setattr("embry0.workflows.qa.orchestrator_report.upsert_sticky_comment", written_comment)
    resolver = AsyncMock()
    resolver.resolve_forge = AsyncMock(return_value=SimpleNamespace(secret="registry-tok"))
    state = _state()
    state["repo"] = None

    out = await qa_report_node(state, {"configurable": {"github_token": "tok", "credential_resolver": resolver}})

    assert out == {}
    written_comment.assert_not_awaited()
    assert resolver.resolve_forge.await_args.args == ("", "github"), resolver.resolve_forge.await_args
