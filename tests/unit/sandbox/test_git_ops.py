"""The sandbox's git config makes the git-proxy its remote for the job's repository (#521).

The command runs in a sandbox shell, so every value it interpolates is checked
against a strict format first. The last tests run the command for real, in a
throwaway HOME, and read back what git stored.
"""

import os
import shutil
import subprocess

import pytest

from embry0.sandbox.github.git_ops import build_sandbox_git_proxy_config_cmd

PROXY = "http://git-proxy:9101"
TOKEN = "a" * 43  # 43 URL-safe chars — within the [40,80] range
ORIGIN = "https://github.com"
REPO = "acme/widgets"


def _cmd(**overrides: str) -> str:
    args = {"git_proxy_url": PROXY, "sandbox_token": TOKEN, "origin": ORIGIN, "repo": REPO, **overrides}
    return build_sandbox_git_proxy_config_cmd(
        args["git_proxy_url"], args["sandbox_token"], origin=args["origin"], repo=args["repo"]
    )


def test_the_command_rewrites_the_repo_to_the_proxy_and_sends_the_bearer_there():
    cmd = _cmd()
    assert "'url.http://git-proxy:9101/git/acme/widgets.git.insteadOf' 'https://github.com/acme/widgets.git'" in cmd
    assert f"'http.http://git-proxy:9101/git/.extraHeader' 'Authorization: Bearer {TOKEN}'" in cmd


def test_the_command_no_longer_installs_a_credential_helper():
    cmd = _cmd()
    assert "/git-credentials" not in cmd, "the sandbox must not ask the proxy for the forge token"
    assert "--unset-all credential.helper" in cmd, "a helper left by an older orchestrator must be removed"


@pytest.mark.parametrize(
    "bad_url",
    ["http://evil.com/x; rm -rf /; #", "", "https://secure.com:443", "not-a-url", "http://git-proxy:9101/"],
)
def test_rejects_a_bad_proxy_url(bad_url):
    with pytest.raises(ValueError, match="git_proxy_url must match"):
        _cmd(git_proxy_url=bad_url)


@pytest.mark.parametrize("bad_token", ["tooshort", "a" * 30 + "; rm -rf /; #" + "a" * 20, "a" * 81, "a" * 39 + "'"])
def test_rejects_a_bad_token(bad_token):
    with pytest.raises(ValueError, match="sandbox_token must match"):
        _cmd(sandbox_token=bad_token)


@pytest.mark.parametrize(
    "bad_origin",
    ["github.com", "ftp://github.com", "https://github.com/", "https://github.com/a b", "https://x.com';id;'", ""],
)
def test_rejects_a_bad_origin(bad_origin):
    with pytest.raises(ValueError, match="origin must be"):
        _cmd(origin=bad_origin)


@pytest.mark.parametrize("bad_repo", ["widgets", "acme/../other", "acme/./w", "acme//w", "acme/w'", "/acme/w", "a/b c"])
def test_rejects_a_bad_repo(bad_repo):
    with pytest.raises(ValueError, match="repo must be"):
        _cmd(repo=bad_repo)


def test_accepts_a_self_managed_origin_and_a_nested_project():
    cmd = _cmd(origin="https://git.example.com:8443/gitlab", repo="group/sub/project")
    assert "'https://git.example.com:8443/gitlab/group/sub/project.git'" in cmd


needs_git = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash")


def _env(home: str) -> dict[str, str]:
    return {"HOME": home, "PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"}


def _run(cmd: str, home: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", cmd], env=_env(home), capture_output=True, text=True, check=False)


def _config(home: str) -> list[str]:
    listing = subprocess.run(["git", "config", "--global", "--list"], env=_env(home), capture_output=True, text=True)
    return listing.stdout.splitlines()


@needs_git
def test_running_the_command_twice_leaves_one_rewrite_one_header_and_no_helper(tmp_path):
    """A resume rewrites the bearer, and a pre-built image carries an older config: neither may stack."""
    home = str(tmp_path)
    old_helper = "!f() { curl -sf http://git-proxy:9101/git-credentials; }; f"
    subprocess.run(["git", "config", "--global", "credential.helper", old_helper], env=_env(home), check=True)

    first = _run(_cmd(), home)
    assert first.returncode == 0, first.stderr
    second = _run(_cmd(sandbox_token="b" * 43), home)
    assert second.returncode == 0, second.stderr

    lines = _config(home)
    assert not [line for line in lines if line.startswith("credential.helper")], lines
    rewrites = [line for line in lines if ".insteadof=" in line]
    assert rewrites == ["url.http://git-proxy:9101/git/acme/widgets.git.insteadof=https://github.com/acme/widgets.git"]
    headers = [line for line in lines if "extraheader" in line]
    assert headers == [f"http.http://git-proxy:9101/git/.extraheader=Authorization: Bearer {'b' * 43}"]


@needs_git
def test_the_command_succeeds_on_a_fresh_home_with_no_gitconfig(tmp_path):
    result = _run(_cmd(), str(tmp_path))
    assert result.returncode == 0, f"unset of an absent helper must not fail the chain: {result.stderr}"
