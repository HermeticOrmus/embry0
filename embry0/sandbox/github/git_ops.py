"""Git configuration for a sandbox container: the git-proxy is the remote (#521, ADR 0007).

The sandbox's git reaches the job's repository through the git-proxy, which
adds the forge credential to each upstream request. The credential never
enters the sandbox: not its env, not a credential helper's answer, not a file.
"""

import re

import structlog

logger = structlog.get_logger(__name__)


# Strict format for git proxy URLs: http://host:port (no path, no query).
# ProxyManager now produces Docker DNS names (e.g. http://git-proxy:9101) that
# resolve inside the sandbox-restricted DinD network. We validate defensively so
# that a future change to the source of the URL can't introduce shell injection
# via the `git config` command we build from it.
_GIT_PROXY_URL_RE = re.compile(r"^http://[a-zA-Z0-9.\-]+:\d+$")

# Strict regex for sandbox bearer tokens. Allows only URL-safe base64 characters
# (produced by secrets.token_urlsafe), preventing any shell metacharacters from
# appearing in the single-quoted config values.
_SANDBOX_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,80}$")

# The forge base URL the sandbox clones from (workspace_init.git.clone_origin)
# and the repository path. Same posture: nothing shell-active can pass.
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~-]+)*$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}(/[A-Za-z0-9_.-]{1,255}){1,19}$")

# The proxy route the git requests go to (embry0.execution.proxy.git_scope.GIT_ROUTE_PREFIX).
_PROXY_GIT_ROUTE = "/git/"

# `git config --unset-all` exits 5 when there is nothing to unset.
_UNSET_HELPER = '{ git config --global --unset-all credential.helper; rc=$?; [ "$rc" -eq 0 ] || [ "$rc" -eq 5 ]; }'


def build_sandbox_git_proxy_config_cmd(git_proxy_url: str, sandbox_token: str, *, origin: str, repo: str) -> str:
    """Bash that makes the git-proxy the sandbox's remote for ``repo``.

    Three ``git config --global`` steps, chained with ``&&`` and safe to run
    again (a resume rewrites the bearer; a pre-built image carries an old one):

    1. Drop any credential helper. Older sandboxes asked the proxy for the
       forge token through one; nothing should prompt for credentials now.
    2. ``url.<proxy>/git/<repo>.git.insteadOf <origin>/<repo>.git``: every
       fetch, push and ``ls-remote`` of the job's repository goes through the
       proxy, while ``origin`` keeps its forge URL. Other repositories are
       untouched and reach their forge directly, without a credential.
    3. ``http.<proxy>/git/.extraHeader``: the per-sandbox bearer, sent to the
       proxy and to nothing else.

    Returns one command with no trailing semicolon, for chaining with ``&&``.

    Raises:
        ValueError: if any argument does not match its strict format.
    """
    if not _GIT_PROXY_URL_RE.fullmatch(git_proxy_url):
        raise ValueError(f"git_proxy_url must match http://host:port (got: {git_proxy_url!r})")
    if not _SANDBOX_TOKEN_RE.fullmatch(sandbox_token):
        raise ValueError("sandbox_token must match ^[A-Za-z0-9_-]{40,80}$")
    if not _ORIGIN_RE.fullmatch(origin):
        raise ValueError(f"origin must be an http(s) base URL (got: {origin!r})")
    if not _REPO_RE.fullmatch(repo) or any(part in (".", "..") for part in repo.split("/")):
        raise ValueError(f"repo must be a repository path like owner/name (got: {repo!r})")
    proxy_base = f"{git_proxy_url}{_PROXY_GIT_ROUTE}"
    # Every interpolated value passed a regex without quotes or shell-active
    # characters, so single quotes are enough.
    return (
        f"{_UNSET_HELPER}"
        f" && git config --global --replace-all 'url.{proxy_base}{repo}.git.insteadOf' '{origin}/{repo}.git'"
        f" && git config --global --replace-all 'http.{proxy_base}.extraHeader'"
        f" 'Authorization: Bearer {sandbox_token}'"
    )
