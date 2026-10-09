# Claude CLI Session File Layout (External Contract)

> **Verified on claude-code 2.1.292** (2026-10-08, #501): the CLI bundled
> in `claude-agent-sdk` 0.2.164, which is the CLI every agent runs. A CLI
> upgrade is an SDK upgrade. When upgrading, check the layout below on the
> new CLI (`tests/integration/test_claude_cli_session_layout.py` is still a
> scaffold). If it moved, update this doc + `claude_cli_session.py`'s
> discovery hierarchy.

## What is this?

The Claude CLI (bundled in the `claude-agent-sdk` wheel the sandbox images
pin) writes per-session conversation state to disk as JSONL. embry0's
conversation-continuity layer (Plan C) needs to:

1. **Discover** session files at end-of-run, to capture conversation state.
2. **Restore** session blobs at start-of-resume, by writing them to the path
   the CLI will look in.

Both operations route through `embry0/agents/claude_cli_session.py` —
the single source of truth.

## Current on-disk layout (claude-code 2.1.292; unchanged since 2.1.92)

```
~/.claude/
└── projects/
    └── <sanitized-cwd>/
        └── <session_id>.jsonl
```

- `<sanitized-cwd>` = the working directory at session creation time,
  with `/` replaced by `-`. E.g., `/workspace/macro-lab` →
  `-workspace-macro-lab`.
- `<session_id>` = the UUID-style session identifier returned by the CLI
  in the first `SystemMessage` (also accessible via `--print-session-id`
  or as `result.session_id` in the SDK).
- File contents = JSONL, one event per line (user message, assistant
  message, tool calls, etc.).

## Saved cost totals (claude-code 2.1.277+)

At exit a headless run appends its session's running totals:

```json
{"type": "cost-state", "sessionId": "<id>", "totalCostUSD": 0.0045, "modelUsage": {...}}
```

A resume restores the last such line, so the resumed run's
`total_cost_usd` includes every earlier run of the session (measured on
2.1.292: 0.0045, then 0.009 for an identical second run).
`restored_cost_usd()` reads it, and `SdkAgentExecutor` subtracts it, so a
run reports only its own cost. The per-run `usage` token counts are not
restored.

## Legacy layout (pre-2.0 CLI)

```
~/.claude/
└── sessions/
    └── <session_id>.jsonl
```

The discovery function still falls back to this for environments running
older CLI versions. New writes go to the projects layout.

## Discovery hierarchy

`find_session_file(home_dir, session_id, project_cwd=None)` tries:

1. `~/.claude/projects/<sanitize(project_cwd)>/<session_id>.jsonl` — fastest, only when cwd known.
2. `~/.claude/projects/*/<session_id>.jsonl` — glob fallback, when cwd unknown.
3. `~/.claude/sessions/<session_id>.jsonl` — legacy layout.

## When to update this doc

- The CLI moves the file (e.g., `chats/`, `conversations/`).
- The sanitization rule changes (e.g., URL-encoding instead of dash-replacement).
- The file format changes (e.g., binary instead of JSONL).

In all cases, update `claude_cli_session.py` first; update this doc to
match; rerun the integration test.
