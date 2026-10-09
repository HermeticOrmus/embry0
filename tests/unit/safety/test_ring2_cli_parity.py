"""Both executors refuse the same file-tool calls (#508), measured on the real CLI.

The SDK executor renders the Ring-2 rules into ``.claude/settings.json`` and the
Claude CLI enforces them; the direct executor checks the same rules with
evaluate_ring2_paths. This test runs the CLI the SDK bundles against a scripted
stand-in for the Messages API on localhost, makes it call each tool in a table,
and requires that the CLI and evaluate_ring2_paths both give the expected
verdict for every row.

The rules are the baseline policy's, rendered by render_settings_json. Only the
``//workspace`` rules are moved to the test's own project directory, so that no
row can write outside ``tmp_path`` even when a rule fails to match. Rows outside
the tree only read (/etc, /proc) or edit with a string that is not in the file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from embry0.agents.config_builder import build_sdk_options
from embry0.agents.invocation import AgentInvocation
from embry0.safety.policy import SafetyPolicy, default_policy_for_agent, evaluate_ring2_paths, render_settings_json

# What the CLI says when a permission rule, not the filesystem, refuses a call.
_DENIAL_MARKERS = (
    "denied by your permission settings",
    "deny rule in your permission settings",
    "has been denied",
)
_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
_MODEL = "claude-sonnet-4-6"


def _bundled_cli() -> Path:
    import claude_agent_sdk

    return Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.access(_bundled_cli(), os.X_OK),
    reason="needs Linux (/etc, /proc) and the CLI bundled with claude_agent_sdk",
)


# ---- a scripted Messages API ---------------------------------------------


class _ScriptedApi:
    """Answers the CLI's n-th main-loop request with the n-th scripted tool call.

    A main-loop request is one that offers tools; side requests (titles and the
    like) get a short text reply. Every tool_result the CLI sends back is kept,
    keyed by the tool_use id the script gave it.
    """

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls
        self.results: dict[str, str] = {}
        self._turn = 0
        self._lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802 - http.server's name
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                if not self.path.split("?")[0].endswith("/v1/messages"):
                    self._send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": "x"}})
                    return
                api._record(body.get("messages") or [])
                blocks = api._next_blocks() if body.get("tools") else [{"type": "text", "text": "ok"}]
                self._send_message(body, blocks)

            def _send_json(self, code: int, obj: dict[str, Any]) -> None:
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_message(self, body: dict[str, Any], blocks: list[dict[str, Any]]) -> None:
                stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
                usage = {"input_tokens": 10, "output_tokens": 10}
                message = {"id": "msg_x", "type": "message", "role": "assistant", "model": body.get("model", _MODEL)}
                if not body.get("stream"):
                    self._send_json(200, {**message, "content": blocks, "stop_reason": stop, "usage": usage})
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                start = {**message, "content": [], "stop_reason": None, "usage": usage}
                events: list[tuple[str, dict[str, Any]]] = [("message_start", {"message": start})]
                for i, b in enumerate(blocks):
                    if b["type"] == "text":
                        first, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": b["text"]}
                    else:
                        first = {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}
                        delta = {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}
                    events += [
                        ("content_block_start", {"index": i, "content_block": first}),
                        ("content_block_delta", {"index": i, "delta": delta}),
                        ("content_block_stop", {"index": i}),
                    ]
                events += [
                    ("message_delta", {"delta": {"stop_reason": stop}, "usage": {"output_tokens": 10}}),
                    ("message_stop", {}),
                ]
                for name, data in events:
                    chunk = f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n".encode()
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _record(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                text = block.get("content")
                if isinstance(text, list):
                    text = " ".join(part.get("text", "") for part in text if isinstance(part, dict))
                with self._lock:
                    self.results[block["tool_use_id"]] = str(text)

    def _next_blocks(self) -> list[dict[str, Any]]:
        with self._lock:
            turn, self._turn = self._turn, self._turn + 1
        if turn >= len(self.calls):
            return [{"type": "text", "text": "done"}]
        name, tool_input = self.calls[turn]
        return [{"type": "tool_use", "id": f"toolu_{turn:03d}", "name": name, "input": tool_input}]


@pytest.fixture
def scripted_api() -> Iterator[Callable[[list[tuple[str, dict[str, Any]]]], _ScriptedApi]]:
    servers: list[_ScriptedApi] = []

    def make(calls: list[tuple[str, dict[str, Any]]]) -> _ScriptedApi:
        api = _ScriptedApi(calls)
        threading.Thread(target=api.server.serve_forever, daemon=True).start()
        servers.append(api)
        return api

    yield make
    for api in servers:
        api.server.shutdown()
        api.server.server_close()


# ---- the table -------------------------------------------------------------


def _relocated(policy: SafetyPolicy, project: Path) -> SafetyPolicy:
    """The policy with its //workspace rules pointed at ``project``."""

    def move(rule: str) -> str:
        return rule.replace("(//workspace/", f"(/{project}/")

    return SafetyPolicy(
        allowed_tools=policy.allowed_tools,
        allow_rules=[move(r) for r in policy.allow_rules],
        deny_rules=[move(r) for r in policy.deny_rules],
    )


def _table(project: Path, home: Path, outside: Path) -> list[tuple[str, dict[str, Any], bool]]:
    """(tool, input, refused). The settings.json write comes last: it would change the rules."""
    p, h, o = str(project), str(home), str(outside)
    miss = {"old_string": "not-in-the-file-508", "new_string": "x"}
    return [
        ("Read", {"file_path": f"{h}/.claude/credentials.json"}, True),  # ~ rules matched before #508
        ("Read", {"file_path": "/etc/passwd"}, True),
        ("Read", {"file_path": "/proc/self/environ"}, True),
        ("Read", {"file_path": "/proc/self/status"}, True),
        ("Glob", {"pattern": "*", "path": "/etc"}, True),
        ("Glob", {"pattern": "/etc/pass*"}, True),
        ("Glob", {"pattern": "*", "path": "/proc/self"}, True),
        ("Grep", {"pattern": "root", "path": "/etc/passwd"}, True),
        ("Grep", {"pattern": "DISABLE_AUTOUPDATER", "path": "/proc/self/environ", "output_mode": "content"}, True),
        ("Edit", {"file_path": "/etc/passwd", **miss}, True),
        ("Edit", {"file_path": f"{p}/.claude/settings.json", **miss}, True),
        ("Write", {"file_path": f"{p}/.claude/settings.local.json", "content": "{}"}, True),
        ("Write", {"file_path": f"{h}/.ssh/authorized_keys", "content": "x"}, True),
        ("Read", {"file_path": f"{p}/README.md"}, False),
        ("Read", {"file_path": f"{p}/.claude/settings.json"}, False),
        ("Read", {"file_path": f"{p}/etc/notes.txt"}, False),
        ("Read", {"file_path": f"{p}/proc/notes.txt"}, False),
        ("Read", {"file_path": f"{o}/notes.txt"}, False),
        ("Glob", {"pattern": "**/*.md", "path": p}, False),
        ("Grep", {"pattern": "readme-508", "path": p}, False),
        ("Edit", {"file_path": f"{p}/README.md", **miss}, False),
        ("Write", {"file_path": f"{p}/src/new.py", "content": "x"}, False),
        ("Write", {"file_path": f"{p}/.claude/skills/probe/SKILL.md", "content": "x"}, False),
        ("Write", {"file_path": f"{p}/.claude/commands/probe.md", "content": "x"}, False),
        ("Write", {"file_path": f"{p}/CLAUDE.md", "content": "x"}, False),
        ("Write", {"file_path": f"{p}/.qa/screenshots/home.txt", "content": "x"}, False),
        ("Write", {"file_path": f"{p}/deliverables/report.md", "content": "x"}, False),
        ("Write", {"file_path": f"{o}/new.txt", "content": "x"}, False),
        ("Write", {"file_path": f"{p}/.claude/settings.json", "content": "{}"}, True),
    ]


def test_the_cli_runs_as_the_sdk_executor_runs_it() -> None:
    """The flags below mirror build_sdk_options; if those change, this test's CLI run must too."""
    invocation = AgentInvocation(
        agent_type="developer",
        prompt="x",
        system_prompt="",
        system_context="",
        model=_MODEL,
        tools=_TOOLS,
        skills=[],
        mcp_servers={},
        max_turns=5,
        timeout_seconds=60,
        execution_mode="sdk",
        auth_mode="api_key",
        safety_policy=default_policy_for_agent("developer"),
        channel_config=None,
    )
    options = build_sdk_options(invocation)
    assert options.permission_mode == "bypassPermissions"
    assert options.setting_sources == ["project"]
    assert str(options.cwd) == "/workspace"


def test_the_cli_and_evaluate_ring2_paths_refuse_the_same_calls(tmp_path: Path, scripted_api: Any) -> None:
    project, home, outside = tmp_path / "workspace", tmp_path / "home", tmp_path / "outside"
    for d in (project / ".claude", project / "etc", project / "proc", home / ".claude", outside):
        d.mkdir(parents=True)
    (project / "README.md").write_text("readme-508\n")
    (project / "etc" / "notes.txt").write_text("notes\n")
    (project / "proc" / "notes.txt").write_text("notes\n")
    (outside / "notes.txt").write_text("notes\n")
    (home / ".claude" / "credentials.json").write_text("{}\n")

    policy = _relocated(default_policy_for_agent("developer"), project)
    (project / ".claude" / "settings.json").write_text(json.dumps(render_settings_json(policy), indent=2))

    table = _table(project, home, outside)
    api = scripted_api([(tool, tool_input) for tool, tool_input, _ in table])
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "ANTHROPIC_BASE_URL": api.url,
        "ANTHROPIC_API_KEY": "sk-ant-api03-ring2-parity-not-a-key",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    cmd = [
        str(_bundled_cli()),
        "-p",
        "probe",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--setting-sources",
        "project",
        "--allowedTools",
        ",".join(_TOOLS),
        "--model",
        _MODEL,
        "--max-turns",
        str(len(table) + 5),
    ]
    proc = subprocess.run(cmd, cwd=project, env=env, capture_output=True, text=True, timeout=300, check=False)
    assert len(api.results) == len(table), (
        f"the CLI answered {len(api.results)} of {len(table)} tool calls (exit {proc.returncode}); "
        f"stderr: {proc.stderr[-2000:]}"
    )

    mismatches = []
    for i, (tool, tool_input, refused) in enumerate(table):
        result = api.results[f"toolu_{i:03d}"]
        cli_refused = any(marker in result for marker in _DENIAL_MARKERS)
        verdict = evaluate_ring2_paths(policy, tool, tool_input, cwd=str(project), home=str(home))
        if cli_refused is not refused or verdict.allowed is refused:
            mismatches.append(
                f"{tool} {json.dumps(tool_input)[:100]}: want {'refused' if refused else 'allowed'}; "
                f"CLI {'refused' if cli_refused else 'allowed'} ({result[:120]!r}); "
                f"evaluate_ring2_paths {'allowed' if verdict.allowed else 'refused'}"
            )
    assert not mismatches, "Ring 2 disagrees with the expected verdicts:\n" + "\n".join(mismatches)
    readme = next(i for i, (tool, ti, _) in enumerate(table) if ti.get("file_path") == f"{project}/README.md")
    assert "readme-508" in api.results[f"toolu_{readme:03d}"], "an allowed Read must return the file's text"
