"""QA ``mode: dind`` needs a sandbox runtime that hands sandboxes a Docker daemon (#268).

In ``mode: dind`` the target app's ``docker compose`` runs on the daemon that
runs the sandboxes. ``AGENT_RUNTIME=rootless`` never gives a sandbox that
daemon, so a QA run in that mode is refused before anything is created,
with the reason, instead of failing at its first ``docker compose``.
"""

from __future__ import annotations

from typing import Any


def dind_mode_refusal(runtime: Any, *, mode: str | None, dind_profile: bool) -> str | None:
    """Why this QA run cannot start on ``runtime``, or None when it can.

    ``mode`` is the QA config's mode; ``dind_profile`` is whether its sandbox
    profile asks for the daemon (``dind_enabled``). Either one is the dind path.
    """
    if mode != "dind" and not dind_profile:
        return None
    if runtime.sandbox_docker_access:
        return None
    return (
        f"QA mode: dind is not available on the {runtime.kind!r} sandbox runtime, which never gives "
        "a sandbox a Docker daemon. Use mode: process (docs/qa-yaml-reference.md), or run this "
        "deployment with AGENT_RUNTIME=dind."
    )
