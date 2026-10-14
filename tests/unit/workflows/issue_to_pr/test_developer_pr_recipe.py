"""RAV-1247 / RAV-1282: the developer prompt's PR contract.

RAV-1247 taught the prompt a credential-helper recipe so agents stopped
burning ~9 turns rediscovering auth (job-54b89ff5c429). RAV-1282 found
that recipe was itself blocked by the sandbox safety policy
(`\\bgit\\s+credential\\b`) — every agent that followed it was denied
(job-2ae81464f3b1). The prompt now says PR creation is impossible from
the sandbox and to lean on the orchestrator fallback; follow-up jobs
that already carry a PR must NOT be told to create one.
"""

from embry0.workflows.issue_to_pr.nodes._developer import _build_developer_full_prompt

_STATE = {
    "repo": "acme/backend",
    "task": "Fix the widget",
    "issue_number": 12,
    "triage_decision": {},
}


def test_full_prompt_states_pr_is_orchestrator_side():
    prompt = _build_developer_full_prompt(dict(_STATE), "embry0/iss-abc-fix")
    # RAV-1282: never instruct a policy-blocked command.
    assert "git credential" not in prompt
    assert "CANNOT create a pull request" in prompt
    assert '"pr_url": null' in prompt
    # The fallback contract: the agent must not burn turns on PR attempts.
    assert "the orchestrator opens the PR automatically" in prompt


def test_existing_pr_job_gets_no_recipe():
    state = {**_STATE, "pr_url": "https://github.com/acme/backend/pull/442"}
    prompt = _build_developer_full_prompt(state, "embry0/iss-abc-fix")
    assert "do NOT create a new PR" in prompt
    assert "CANNOT create a pull request" not in prompt


def test_full_prompt_ships_a_spec_with_the_code_it_describes():
    """A design spec or plan rides in the job's branch and PR, never a PR of its own."""
    prompt = _build_developer_full_prompt(dict(_STATE), "embry0/iss-abc-fix")
    assert "commit it on this branch with the code" in prompt, prompt


# ── RAV-1254: dev_checks self-check block (ex .e0/dev.yaml) ─────────────────


def test_full_prompt_renders_dev_checks_block():
    state = {
        **_STATE,
        "dev_checks": [
            {"name": "python", "paths": ["ml"], "command": "cd ml && poetry run pytest -q"},
            {"name": "node", "paths": ["frontend/", "shared"], "command": "cd frontend && npm test"},
            {"name": "global-lint", "paths": [], "command": "make lint"},
        ],
    }
    prompt = _build_developer_full_prompt(state, "embry0/iss-abc-fix")
    assert "Self-check before opening the PR" in prompt
    assert "- python (if you changed files under ml/): `cd ml && poetry run pytest -q`" in prompt
    assert "- node (if you changed files under frontend/, shared/): `cd frontend && npm test`" in prompt
    assert "- global-lint (always): `make lint`" in prompt
    # The old in-repo convention must not be referenced anywhere.
    assert ".e0/dev.yaml" not in prompt


def test_full_prompt_omits_dev_checks_block_when_absent():
    prompt = _build_developer_full_prompt(dict(_STATE), "embry0/iss-abc-fix")
    assert "Self-check before opening the PR" not in prompt


def test_dev_checks_block_skips_malformed_entries():
    from embry0.workflows.issue_to_pr.nodes._developer import _format_dev_checks_block

    assert _format_dev_checks_block(None) == ""
    assert _format_dev_checks_block("not-a-list") == ""
    assert _format_dev_checks_block([{"name": "no-command"}, "garbage"]) == ""
    block = _format_dev_checks_block([{"name": "ok", "paths": None, "command": "make check"}, {"bad": 1}])
    assert "- ok (always): `make check`" in block
