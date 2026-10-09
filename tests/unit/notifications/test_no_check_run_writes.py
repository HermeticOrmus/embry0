"""Guard: embry0 must never write to GitHub's Checks API.

GitHub accepts create/update on ``/repos/{owner}/{repo}/check-runs`` only from
a GitHub App installation token. embry0 authenticates with personal access
tokens, so every such write is rejected with 403 ("You must authenticate via a
GitHub App") for every repo, unconditionally — it is not a transient or
per-repo failure and it is not retryable. QA results are reported through the
sticky PR comment instead.

Reading check runs (``GET .../commits/{sha}/check-runs``, used by merge
approvals to gate on other tools' CI) is unrestricted and stays allowed — this
guard only bans writes.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "embry0"

WRITE_METHODS = {"post", "patch", "put", "delete"}


def _url_literal(node: ast.expr) -> str | None:
    """Best-effort static read of a URL argument, including f-strings."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("\x00")  # placeholder for an interpolated expression
        return "".join(parts)
    return None


def _check_run_writes(source: str) -> list[tuple[int, str]]:
    """Return (lineno, url) for every HTTP write call targeting check-runs."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in WRITE_METHODS:
            continue
        url = _url_literal(node.args[0])
        if url is None or "check-runs" not in url:
            continue
        found.append((node.lineno, url))
    return found


def test_no_module_writes_to_the_checks_api():
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for lineno, url in _check_run_writes(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(PACKAGE_ROOT.parent)}:{lineno} -> {url}")

    assert not offenders, (
        "Checks API write found; GitHub rejects these with 403 unless the caller "
        "is a GitHub App installation, which embry0 is not. Report QA results "
        "through the sticky PR comment instead. Offenders:\n  " + "\n  ".join(offenders)
    )


def test_guard_detects_a_check_run_write():
    """The guard must be able to fail — prove it flags a real write call."""
    offending = 'await client.post(f"/repos/{repo}/check-runs", repo=repo, json=body)\n'
    assert _check_run_writes(offending), "guard failed to flag a check-run POST"

    patch_call = 'await client.patch(f"/repos/{repo}/check-runs/{existing_id}", json=b)\n'
    assert _check_run_writes(patch_call), "guard failed to flag a check-run PATCH"

    reading = 'await client.get(f"/repos/{repo}/commits/{sha}/check-runs", repo=repo)\n'
    assert not _check_run_writes(reading), "guard wrongly flagged a read"
