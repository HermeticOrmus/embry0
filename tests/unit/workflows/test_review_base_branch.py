"""The review diff base must not be a guess (RAV-1405 + Spec 1A §3.7).

Spec 1A §3.7 made ``init_node`` populate ``state["base_branch"]`` for every
job, and the services-side resolver never returns empty. Feeding that value
straight into ``workspace_diff.resolve_base_branch`` short-circuits it on its
first line, making its ``origin/HEAD`` symref step unreachable — so when the
forge step was unavailable (no token, API error), a free and correct symref
answer was replaced by the literal "main". Exactly what the QA orchestrator's
``_configured_base_branch`` was written to prevent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embry0.services.base_branch import FALLBACK_SOURCE
from embry0.workflows.issue_to_pr.nodes._review import _configured_base_branch


def _docker(symref: str):
    """A sandbox whose clone knows its default branch, and an empty diff."""
    docker = MagicMock()
    docker.build_exec_cmd = MagicMock(side_effect=lambda container_id, cmd: ["docker", "exec", *cmd])

    async def run_cmd(cmd, timeout=None):
        joined = " ".join(str(c) for c in cmd)
        if "symbolic-ref" in joined:
            return f"origin/{symref}\n"
        return ""

    docker.run_cmd = AsyncMock(side_effect=run_cmd)
    return docker


def test_a_resolver_fallback_counts_as_unconfigured():
    assert _configured_base_branch({"base_branch": "main", "base_branch_source": FALLBACK_SOURCE}) is None


def test_a_real_answer_is_kept():
    for source in ("override", "preference", "forge"):
        assert _configured_base_branch({"base_branch": "dev", "base_branch_source": source}) == "dev"


def test_a_missing_source_label_is_treated_as_configured():
    """A job checkpointed before the key existed keeps the old behaviour —
    the safe side, since a real preference must never lose to the symref."""
    assert _configured_base_branch({"base_branch": "dev"}) == "dev"


async def _run_review(state: dict, docker) -> str:
    """Drive review_node and return the prompt handed to the agent."""
    from embry0.workflows.issue_to_pr.nodes import review_node

    seen: dict = {}

    async def _run(*args, **kwargs):
        seen["prompt"] = kwargs["prompt"]
        return {
            "agent_outputs": [
                {
                    "agent_type": "review",
                    "is_error": False,
                    "output": '{"decision": "approved", "summary": "lgtm"}',
                    "messages": [],
                    "mode": "anthropic_api",
                }
            ],
            "events": [],
            "total_cost_usd": 0.0,
        }

    with (
        patch("embry0.workflows.issue_to_pr.nodes.run_agent_node", new=AsyncMock(side_effect=_run)),
        patch("embry0.workflows.issue_to_pr.nodes.get_stream_writer", return_value=lambda _: None),
    ):
        await review_node(
            state,
            {"configurable": {"agent_runner": object(), "credentials": {}, "docker": docker}},
        )
    return str(seen["prompt"])


def _state(**extra) -> dict:
    return {
        "job_id": "J",
        "issue_id": "iss",
        "repo": "o/r",
        "task": "x",
        "sandbox_container_id": "C",
        "branch_name": "embry0/test",
        "agent_outputs": [],
        "errors": [],
        "pipeline_config": {"agent_models": {"review": "claude-sonnet-4-6"}, "budget_usd": 50.0},
        **extra,
    }


@pytest.mark.asyncio
async def test_review_lets_the_symref_answer_when_the_resolver_only_guessed():
    prompt = await _run_review(
        _state(base_branch="main", base_branch_source=FALLBACK_SOURCE),
        _docker("develop"),
    )
    assert "git diff develop...HEAD" in prompt
    assert "git diff main...HEAD" not in prompt


@pytest.mark.asyncio
async def test_review_honours_a_real_preference_over_the_symref():
    prompt = await _run_review(
        _state(base_branch="dev", base_branch_source="preference"),
        _docker("develop"),
    )
    assert "git diff dev...HEAD" in prompt
    assert "git diff develop...HEAD" not in prompt
