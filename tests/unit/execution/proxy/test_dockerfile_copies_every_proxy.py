"""Dockerfile.proxy copies proxy modules file-by-file (#166).

A new proxy module that nobody adds a COPY line for simply does not exist in
the image: `PROXY_TYPE=gitlab` would then fail at import inside DinD, far from
the change that caused it. This pins the two lists together.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PROXY_DIR = _REPO_ROOT / "embry0" / "execution" / "proxy"
_DOCKERFILE = _REPO_ROOT / "infra" / "Dockerfile.proxy"


# Modules that live under proxy/ but run in the ORCHESTRATOR, not inside the
# proxy image. manager.py drives docker to launch the proxies; shipping it into
# the image would be wrong, not merely unnecessary.
_ORCHESTRATOR_SIDE = {"manager.py"}


def test_every_proxy_module_has_a_copy_line() -> None:
    dockerfile = _DOCKERFILE.read_text()
    modules = sorted(p.name for p in _PROXY_DIR.glob("*.py") if p.name not in _ORCHESTRATOR_SIDE)
    missing = [m for m in modules if f"proxy/{m}" not in dockerfile]
    assert not missing, (
        f"{missing} exist under embry0/execution/proxy/ but have no COPY line in "
        "infra/Dockerfile.proxy — they will be absent from the proxy image."
    )


def test_the_orchestrator_side_exclusion_is_still_accurate() -> None:
    """If manager.py ever stops existing, this exclusion is stale and hiding gaps."""
    for name in _ORCHESTRATOR_SIDE:
        assert (_PROXY_DIR / name).exists(), f"{name} is excluded but no longer exists"


def test_dockerfile_copies_the_tls_module() -> None:
    """gitlab_proxy imports embry0.execution.tls; absent, PROXY_TYPE=gitlab dies at import."""
    dockerfile = _DOCKERFILE.read_text()
    assert "execution/tls.py" in dockerfile


def _embry0_imports(path: Path) -> set[str]:
    """Dotted embry0 modules ``path`` imports, at any depth in the file."""
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return {n for n in names if n == "embry0" or n.startswith("embry0.")}


def _files_for(module: str) -> list[str]:
    """Repo-relative files importing ``module`` loads: each package __init__, then the module."""
    parts = module.split(".")
    files = ["/".join(parts[:i]) + "/__init__.py" for i in range(1, len(parts))]
    leaf = "/".join(parts)
    files.append(leaf + ".py" if (_REPO_ROOT / f"{leaf}.py").exists() else leaf + "/__init__.py")
    return files


def test_every_embry0_module_a_proxy_imports_is_copied() -> None:
    """A proxy module that imports another embry0 module dies at import inside DinD if the
    image lacks that file. Followed transitively, packages' __init__ included: a package
    __init__ that imports a sibling needs that sibling too."""
    dockerfile = _DOCKERFILE.read_text()
    todo = [p for p in _PROXY_DIR.glob("*.py") if p.name not in _ORCHESTRATOR_SIDE]
    seen: set[str] = set()
    while todo:
        for module in _embry0_imports(todo.pop()):
            for rel in _files_for(module):
                if rel not in seen and (_REPO_ROOT / rel).exists():
                    seen.add(rel)
                    todo.append(_REPO_ROOT / rel)
    missing = sorted(rel for rel in seen if f"COPY {rel} " not in dockerfile)
    assert not missing, f"proxy modules import {missing}, which infra/Dockerfile.proxy does not copy"
