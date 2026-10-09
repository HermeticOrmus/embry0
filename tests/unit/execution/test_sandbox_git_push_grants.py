"""Which sandboxes may push, and where the git-proxy forwards for them (#521).

A sandbox's git reaches its repository through the git-proxy, which pushes
only for an enrollment with ``git_push``. Only the issue-to-pr init node asks
for it, because its developer pushes a branch. QA, onboarding, the volume
warmer and the QA image builder run repository code (installs, boot commands)
and only need to read. The guard below makes granting push anywhere else a
deliberate edit of this test.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from embry0.execution.sandbox_manager import SandboxManager

# Autouse: every sandbox on sandbox-restricted gets the isolation chain (#554),
# which the docker mock cannot write.
from tests.unit.execution.test_sandbox_manager import _stub_isolation_unless_overridden  # noqa: F401

_PACKAGE = Path(__file__).resolve().parents[3] / "embry0"
_PUSHERS = {"workflows/issue_to_pr/nodes/_init.py"}


def _sandbox_create_calls() -> dict[str, list[ast.Call]]:
    """Every ``<sandbox manager>.create(...)`` call in the package, by file."""
    found: dict[str, list[ast.Call]] = {}
    for path in _PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create"):
                continue
            owner = ast.unparse(node.func.value)
            if "sandbox" in owner:
                found.setdefault(str(path.relative_to(_PACKAGE)), []).append(node)
    return found


def test_only_the_issue_to_pr_init_node_creates_a_sandbox_that_pushes():
    calls = _sandbox_create_calls()
    assert len(calls) >= 6, f"the scan lost the sandbox create calls it should see: {sorted(calls)}"
    granting: set[str] = set()
    for path, nodes in calls.items():
        for call in nodes:
            push = next((kw.value for kw in call.keywords if kw.arg == "git_push"), None)
            if push is None:
                continue
            assert isinstance(push, ast.Constant), f"{path}: git_push must be a literal, not {ast.unparse(push)}"
            if push.value is True:
                granting.add(path)
    assert granting == _PUSHERS, f"sandboxes created with the push right: {sorted(granting)}"


def _manager(gitlab_url: str = "") -> tuple[SandboxManager, MagicMock]:
    docker = MagicMock()
    docker.run_cmd = AsyncMock(return_value="container-1")
    docker.build_run_cmd = MagicMock(return_value=["docker", "run", "..."])
    proxy = MagicMock()
    proxy.enroll_sandbox = AsyncMock(return_value="tok-" + "a" * 40)
    proxy.unenroll_sandbox = AsyncMock()
    proxy.internet_proxies = MagicMock(return_value=[])
    mgr = SandboxManager(docker=docker, proxy_manager=proxy, gitlab_url=gitlab_url)
    return mgr, proxy


@pytest.mark.parametrize(
    ("forge", "gitlab_url", "upstream"),
    [
        ("github", "", "https://github.com"),
        ("gitlab", "", "https://gitlab.com"),
        ("gitlab", "https://git.example.com:8443/gitlab/", "https://git.example.com:8443/gitlab"),
    ],
)
async def test_create_enrolls_the_git_proxy_with_the_sandboxs_clone_origin(monkeypatch, forge, gitlab_url, upstream):
    monkeypatch.setattr(
        "embry0.execution.runtime.egress.EgressEnforcer",
        MagicMock(
            return_value=MagicMock(
                container_ip=AsyncMock(return_value="172.30.0.5"), ensure_helper_image=AsyncMock(), apply=AsyncMock()
            )
        ),
    )
    mgr, proxy = _manager(gitlab_url)
    await mgr.create(job_id="job-1", repo="acme/widgets", forge=forge)
    kwargs = proxy.enroll_sandbox.await_args.kwargs
    assert kwargs.get("git_upstream") == upstream, f"the git-proxy must forward to {upstream}: {kwargs}"
    assert kwargs.get("git_push") is False, "a sandbox is read-only unless its creator asks for the push right"

    await mgr.create(job_id="job-2", repo="acme/widgets", forge=forge, git_push=True)
    assert proxy.enroll_sandbox.await_args.kwargs["git_push"] is True
