"""Every SandboxManager.create caller passes llm_providers.

Clone-only callers pass an empty tuple so they never receive an LLM secret.
Derived sandboxes also pass parent_job_id so they reuse the parent's account.
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]

# Not agent jobs. An empty tuple is the whole contract: no LLM secret.
_NO_LLM = {
    "embry0/cache/image_builder.py",
    "embry0/cache/image_builder_cli.py",
    "embry0/cache/volume_warmer.py",
}

# The sandbox id is ``<parent>__…``. The parent id is how the pool stays sticky.
_DERIVED_PARENT = {
    "embry0/workflows/qa/subtask_nodes.py",
    "embry0/workflows/qa/orchestrator.py",
}


def _sandbox_creates(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        func = getattr(node, "func", None)
        if not isinstance(node, ast.Call) or not isinstance(func, ast.Attribute):
            continue
        if func.attr != "create" or not isinstance(func.value, ast.Name):
            continue
        if func.value.id == "sandbox_mgr":
            calls.append(node)
    return calls


def _kw(call: ast.Call, name: str) -> ast.keyword | None:
    return next((kw for kw in call.keywords if kw.arg == name), None)


def test_every_sandbox_create_passes_llm_providers() -> None:
    missing: list[str] = []
    empty_required: list[str] = []
    derived_missing_parent: list[str] = []
    found = 0
    for path in sorted(_ROOT.joinpath("embry0").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        for call in _sandbox_creates(path):
            found += 1
            where = f"{rel}:{call.lineno}"
            providers = _kw(call, "llm_providers")
            if providers is None:
                missing.append(where)
                continue
            if rel in _NO_LLM:
                value = providers.value
                if not isinstance(value, ast.Tuple) or value.elts:
                    empty_required.append(where)
            if rel in _DERIVED_PARENT and _kw(call, "parent_job_id") is None:
                derived_missing_parent.append(where)
            if rel == "embry0/workflows/onboard/nodes.py":
                first = call.args[0] if call.args else None
                if isinstance(first, ast.Name) and first.id == "smoke_job_id" and _kw(call, "parent_job_id") is None:
                    derived_missing_parent.append(where)
    assert found >= 8, f"expected every sandbox_mgr.create in the tree, found {found}"
    assert not missing, f"SandboxManager.create callers missing llm_providers: {missing}"
    assert not empty_required, f"clone-only callers must pass llm_providers=(): {empty_required}"
    assert not derived_missing_parent, f"derived sandboxes must pass parent_job_id: {derived_missing_parent}"
