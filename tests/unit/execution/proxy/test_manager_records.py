"""ProxyManager and the records-proxy: launch gate, scoped enrollment, fail-open."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.execution.proxy.manager import ProxyManager


def _manager(answers: dict[str, Any]) -> tuple[ProxyManager, list[dict[str, Any]]]:
    """A manager with only the records-proxy running; ``answers`` maps container -> reply or exception."""
    pm = ProxyManager(docker=MagicMock(), proxy_admin_token="admin-secret")
    pm._http = MagicMock()
    pm.records_proxy_url = "http://records-proxy:9108"
    calls: list[dict[str, Any]] = []

    async def fake_exec(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs["method"] == "DELETE":
            return {"status": "unenrolled"}
        answer = answers.get(kwargs["container"], {})
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(kwargs["body"])
        return answer

    pm._exec_admin_request = fake_exec  # type: ignore[method-assign]
    return pm, calls


def _echo(body: dict[str, Any]) -> dict[str, Any]:
    return {"status": "enrolled", "allowed_repos": body["allowed_repos"]}


@pytest.mark.asyncio
async def test_the_sandbox_is_enrolled_for_its_own_repo_only() -> None:
    pm, calls = _manager({"records-proxy": _echo})
    token = await pm.enroll_sandbox("sb-1", repo="group/sub/project", forge="gitlab")
    enroll = [c for c in calls if c["container"] == "records-proxy"]
    assert len(enroll) == 1
    assert enroll[0]["port"] == 9108 and enroll[0]["path"] == "/admin/enroll"
    assert enroll[0]["body"]["allowed_repos"] == ["group/sub/project"]
    assert enroll[0]["body"]["sandbox_token"] == token, "one bearer for every proxy"
    assert pm.records_access("sb-1") is True


@pytest.mark.asyncio
async def test_a_job_without_a_repo_gets_an_empty_scope() -> None:
    pm, calls = _manager({"records-proxy": _echo})
    await pm.enroll_sandbox("sb-1")
    assert calls[-1]["body"]["allowed_repos"] == []
    assert pm.records_access("sb-1") is False


@pytest.mark.asyncio
async def test_an_unconfirmed_scope_is_revoked_and_the_sandbox_survives() -> None:
    """An older proxy image ignores allowed_repos; it must not keep the enrollment."""
    pm, calls = _manager({"records-proxy": {"status": "enrolled"}})
    token = await pm.enroll_sandbox("sb-1", repo="acme/widgets")
    assert token
    assert [c["method"] for c in calls if c["container"] == "records-proxy"] == ["POST", "DELETE"]
    assert pm.records_access("sb-1") is False


@pytest.mark.asyncio
async def test_a_failing_records_proxy_never_fails_the_sandbox() -> None:
    pm, _ = _manager({"records-proxy": RuntimeError("container is not running")})
    assert await pm.enroll_sandbox("sb-1", repo="acme/widgets")
    assert pm.records_access("sb-1") is False


@pytest.mark.asyncio
async def test_unenroll_revokes_records_access() -> None:
    pm, calls = _manager({"records-proxy": _echo})
    await pm.enroll_sandbox("sb-1", repo="acme/widgets")
    await pm.unenroll_sandbox("sb-1")
    assert ("records-proxy", "DELETE", "/admin/enroll/sb-1") in {
        (c["container"], c["method"], c["path"]) for c in calls
    }
    assert pm.records_access("sb-1") is False


@pytest.mark.asyncio
async def test_not_running_means_not_enrolled() -> None:
    pm, calls = _manager({})
    pm.records_proxy_url = ""
    await pm.enroll_sandbox("sb-1", repo="acme/widgets")
    assert all(c["container"] != "records-proxy" for c in calls)


@pytest.mark.asyncio
async def test_launched_only_with_the_service_key(monkeypatch) -> None:
    monkeypatch.setattr("embry0.execution.proxy.manager.socket.gethostbyname", lambda host: "10.99.0.1")
    envs: dict[str, dict[str, str]] = {}

    async def fake_launch(*, name: str, env: dict[str, str], attach_internet: bool) -> None:
        envs[name] = env

    for key in ("", "svc-records-key"):
        envs.clear()
        pm = ProxyManager(docker=MagicMock(run_cmd=AsyncMock(return_value="")), proxy_admin_token="x" * 20)
        pm._http = MagicMock()
        monkeypatch.setattr(pm, "_launch", fake_launch)
        await pm.start(records_api_key=key)
        if not key:
            assert "records-proxy" not in envs and pm.records_proxy_url == ""
        else:
            assert envs["records-proxy"]["ORCHESTRATOR_API_KEY"] == key
            assert envs["records-proxy"]["PROXY_TYPE"] == "records"
            assert pm.records_proxy_url == "http://records-proxy:9108"
