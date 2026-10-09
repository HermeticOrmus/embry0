import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from embry0.cli.__main__ import cmd_credentials


def _args(**kw):
    base = {"action": "import-env", "replace": False, "api_url": "http://orch:8000", "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_import_env_posts_and_prints(capsys, monkeypatch):
    monkeypatch.delenv("EMBRY0_API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", "k")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"created": ["github/acme"], "bound": ["owner:github:acme:forge -> cred-1"], "skipped": []}
    with patch("httpx.post", return_value=resp) as post:
        with pytest.raises(SystemExit) as exc:
            cmd_credentials(_args())
    assert exc.value.code == 0
    assert post.call_args.args[0] == "http://orch:8000/api/v1/credentials/import-env"
    assert post.call_args.kwargs["json"] == {"replace": False}
    headers = post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer k"
    # The CSRF middleware 403s any non-safe request without this header.
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    out = capsys.readouterr().out
    assert "created: github/acme" in out
    assert "bound: owner:github:acme:forge -> cred-1" in out
    assert "skipped: -" in out


def test_skipped_exits_2_and_json(capsys, monkeypatch):
    monkeypatch.delenv("EMBRY0_API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", "k")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "created": [],
        "bound": [],
        "skipped": ["github/default: exists with a different secret (pass replace=true)"],
    }
    with patch("httpx.post", return_value=resp) as post:
        with pytest.raises(SystemExit) as exc:
            cmd_credentials(_args(json=True, replace=True))
    assert exc.value.code == 2
    assert post.call_args.kwargs["json"] == {"replace": True}
    assert json.loads(capsys.readouterr().out)["skipped"]


def test_http_error_exits_1(capsys, monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    resp = MagicMock(status_code=403, text="forbidden")
    with patch("httpx.post", return_value=resp):
        with pytest.raises(SystemExit) as exc:
            cmd_credentials(_args())
    assert exc.value.code == 1
    assert "403" in capsys.readouterr().err
