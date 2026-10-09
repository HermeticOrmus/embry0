"""Runner config transport (#304): stdin for the orchestrator, argv kept for
an old orchestrator talking to a new image."""

from __future__ import annotations

import io
import json

import pytest

from embry0.sandbox.runner import _read_config


def test_config_stdin_reads_a_payload_larger_than_one_argv_string(monkeypatch):
    config = {"agent_type": "review", "prompt": "d" * (300 * 1024)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(config)))

    assert _read_config(["--config-stdin"]) == (config, None, None)


def test_config_argv_is_still_accepted():
    assert _read_config(["--config", '{"agent_type": "qa"}', "--session-id", "sess-9"]) == (
        {"agent_type": "qa"},
        None,
        "sess-9",
    )


@pytest.mark.parametrize("argv", [[], ["--config", "{}", "--config-stdin"]])
def test_exactly_one_config_source_is_required(argv):
    with pytest.raises(SystemExit):
        _read_config(argv)
