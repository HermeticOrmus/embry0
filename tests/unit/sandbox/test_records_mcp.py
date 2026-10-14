"""The in-sandbox records MCP server: protocol, proxy calls, paging, errors.

Pure handlers, urllib stubbed: no subprocess, no network.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from embry0.sandbox import records_mcp


@pytest.fixture
def provisioned(monkeypatch):
    monkeypatch.setenv("EMBRY0_RECORDS_PROXY_URL", "http://records-proxy:9108")
    monkeypatch.setenv("EMBRY0_RECORDS_PROXY_TOKEN", "tok-" + "x" * 40)


def _stub(monkeypatch, payload=None, error=None):
    seen: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload or {}).encode()

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        if error is not None:
            raise error
        return _Resp()

    monkeypatch.setattr(records_mcp.urllib.request, "urlopen", fake_urlopen)
    return seen


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", None, io.BytesIO(json.dumps(body).encode()))  # type: ignore[arg-type]


def test_lists_both_tools() -> None:
    reply = records_mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply is not None
    assert {t["name"] for t in reply["result"]["tools"]} == {"records_list", "records_get"}
    assert records_mcp.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_list_calls_the_proxy_without_naming_a_repo(provisioned, monkeypatch) -> None:
    seen = _stub(
        monkeypatch,
        {"records": [{"slug": "outage", "kind": "rca", "title": "Outage", "current_revision": 2, "updated_at": "t"}]},
    )
    text, is_error = records_mcp.run_list({"kind": "rca", "query": "cache"})
    assert not is_error
    assert "outage  [rca]  Outage" in text
    assert seen["url"] == "http://records-proxy:9108/records?kind=rca&q=cache"
    assert seen["headers"]["Authorization"] == "Bearer tok-" + "x" * 40


def test_get_pages_a_long_body(provisioned, monkeypatch) -> None:
    body = "a" * 70_000
    seen = _stub(
        monkeypatch,
        {"slug": "big", "kind": "note", "title": "Big", "revision": 1, "current_revision": 3, "body": body},
    )
    text, is_error = records_mcp.run_get({"slug": "big", "revision": 1})
    assert not is_error
    assert seen["url"] == "http://records-proxy:9108/records/big?revision=1"
    assert "revision 1 of 3" in text
    assert "offset=30000" in text
    text, _ = records_mcp.run_get({"slug": "big", "offset": 60_000})
    assert text.endswith("a" * 10_000) and "offset=" not in text.split("\n")[2]


def test_slug_is_one_path_segment(provisioned, monkeypatch) -> None:
    seen = _stub(monkeypatch, {"body": ""})
    records_mcp.run_get({"slug": "../../other"})
    assert seen["url"] == "http://records-proxy:9108/records/..%2F..%2Fother"


def test_errors_are_tool_errors(provisioned, monkeypatch) -> None:
    _stub(monkeypatch, error=_http_error(404, {"detail": "no record 'x'"}))
    assert records_mcp.run_get({"slug": "x"}) == ("no record 'x'", True)
    _stub(monkeypatch, error=_http_error(401, {"error": "unauthorized"}))
    assert records_mcp.run_list({})[1] is True
    _stub(monkeypatch, error=urllib.error.URLError("refused"))
    assert "unreachable" in records_mcp.run_list({})[0]
    assert records_mcp.run_get({}) == ("records_get requires a `slug` argument", True)


def test_not_provisioned(monkeypatch) -> None:
    monkeypatch.delenv("EMBRY0_RECORDS_PROXY_URL", raising=False)
    monkeypatch.setenv("EMBRY0_RECORDS_PROXY_TOKEN", "tok")
    assert records_mcp.run_list({}) == (records_mcp._NOT_PROVISIONED, True)


def test_unknown_tool_and_crash_still_reply(provisioned, monkeypatch) -> None:
    reply = records_mcp.handle_message(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}
    )
    assert reply is not None and reply["result"]["isError"] is True

    def boom(_args):
        raise RuntimeError("bad")

    monkeypatch.setattr(records_mcp, "run_list", boom)
    reply = records_mcp.handle_message(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "records_list", "arguments": {}}}
    )
    assert reply is not None and reply["id"] == 8 and reply["result"]["isError"] is True


def test_token_file_command_is_0600_and_quoted() -> None:
    argv = records_mcp.build_records_token_write_cmd("a'b")
    assert argv[:2] == ["bash", "-c"]
    assert "chmod 600" in argv[2] and records_mcp.RECORDS_PROXY_TOKEN_REL in argv[2]
    assert "'a'\"'\"'b'" in argv[2]
