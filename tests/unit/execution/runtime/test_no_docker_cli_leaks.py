"""Nothing outside the runtime builds raw docker commands (#268).

``DockerClient._build_base_cmd()`` is the docker CLI prefix: host, TLS and
all. A caller holding it can run any docker command, so it can only ever
run on the Docker CLI. Every caller goes through ``AgentRuntime`` instead,
and the prefix stays with the client that defines it and the runtime that
owns the client.
"""

from __future__ import annotations

import re
from pathlib import Path

import embry0

_PACKAGE = Path(embry0.__file__).resolve().parent
_NAME = "_build_base_cmd"

# Where the prefix may appear: its definition, and the runtime.
_ALLOWED = ("execution/docker_client.py", "execution/runtime/")

# Still reaching for the prefix, to be moved behind the runtime. An entry
# that no longer leaks fails below.
_PENDING: frozenset[str] = frozenset()


def _leaking_files() -> set[str]:
    leaking: set[str] = set()
    for path in _PACKAGE.rglob("*.py"):
        rel = path.relative_to(_PACKAGE).as_posix()
        if rel.startswith(_ALLOWED):
            continue
        if _NAME in path.read_text(encoding="utf-8"):
            leaking.add(rel)
    return leaking


def test_the_scan_reads_the_package_under_test() -> None:
    # An empty scan would pass the guard while checking nothing.
    assert (_PACKAGE / "execution" / "docker_client.py").is_file()
    assert _NAME in (_PACKAGE / "execution" / "docker_client.py").read_text(encoding="utf-8")


def test_no_module_outside_the_runtime_builds_raw_docker_commands() -> None:
    leaking = _leaking_files() - _PENDING
    assert not leaking, (
        f"{sorted(leaking)} call DockerClient.{_NAME}() outside embry0/execution/runtime/. "
        "Add the operation to AgentRuntime (protocol.py) and DockerRuntime instead."
    )


def test_the_pending_list_holds_only_files_that_still_leak() -> None:
    stale = _PENDING - _leaking_files()
    assert not stale, f"{sorted(stale)} no longer call {_NAME}(); remove them from _PENDING"


def test_no_module_reaches_into_another_objects_docker_client() -> None:
    """``getattr(self._sandbox, "_docker", None)`` handed the graph a private
    attribute of SandboxManager (#268). The runtime is the handle now:
    ``sandbox_manager.runtime`` and its ``client``."""
    reach_through = re.compile(r"""(?<!self)\._docker\b|["']_docker["']""")
    found = []
    for path in _PACKAGE.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if reach_through.search(line):
                found.append(f"{path.relative_to(_PACKAGE).as_posix()}:{number}: {line.strip()}")
    assert not found, "use the runtime handle instead of another object's _docker:\n" + "\n".join(found)
