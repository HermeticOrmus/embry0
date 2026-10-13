"""The orchestrator image must ship embry0.mcp_server — the hosted MCP service
(compose `mcp`, console script embry0-mcp-http) runs from that image.

Regression: the entry point installed from pyproject but the module was
absent from the /app source copy, so the service crash-looped with
ModuleNotFoundError at first deploy of #224.

The package lives under embry0/, so the image's `COPY embry0/` carries it.
It stays a thin HTTP client of the orchestrator: it imports nothing from
embry0 outside itself.
"""

import ast
import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).parents[3]
_DOCKERFILE = _ROOT / "infra" / "Dockerfile.orchestrator"
_COMPOSE = _ROOT / "infra" / "docker-compose.yml"
_PACKAGE = _ROOT / "embry0" / "mcp_server"


def _module_file(dotted: str) -> Path:
    return _ROOT.joinpath(*dotted.split(".")).with_suffix(".py")


def test_orchestrator_dockerfile_copies_the_mcp_package() -> None:
    content = _DOCKERFILE.read_text()
    assert "COPY embry0/ /app/embry0/" in content, "the image no longer copies embry0/, which holds the MCP server"
    assert (_PACKAGE / "http.py").is_file(), "embry0/mcp_server/http.py is missing"
    assert "import embry0, embry0.mcp_server.http" in content, "the build-time smoke no longer imports the MCP server"


def test_compose_mcp_command_names_a_module_in_the_tree() -> None:
    commands = re.findall(r'command: \["python", "-m", "([\w.]+)"\]', _COMPOSE.read_text())
    assert "embry0.mcp_server.http" in commands, f"the mcp service does not run embry0.mcp_server.http: {commands}"
    for module in commands:
        assert _module_file(module).is_file(), f"compose runs {module}, which is not in the tree"


def test_mcp_console_scripts_point_at_the_package() -> None:
    scripts = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    for name in ("embry0-mcp", "embry0-mcp-http"):
        module, _, func = scripts[name].partition(":")
        assert module.startswith("embry0.mcp_server."), f"{name} points at {scripts[name]}"
        assert f"def {func}(" in _module_file(module).read_text(), f"{name}: {module} defines no {func}()"


def _imported_names(tree: ast.AST) -> list[tuple[int, str]]:
    names: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append((node.lineno, node.module or ""))
        elif isinstance(node, ast.ImportFrom) and node.level > 1:
            names.append((node.lineno, "." * node.level + (node.module or "")))
    return names


def test_mcp_server_imports_nothing_else_from_embry0() -> None:
    offenders = [
        f"{path.relative_to(_ROOT)}:{line} imports {name}"
        for path in sorted(_PACKAGE.rglob("*.py"))
        for line, name in _imported_names(ast.parse(path.read_text()))
        if (name.split(".")[0] == "embry0" and not (name + ".").startswith("embry0.mcp_server."))
        or name.startswith("..")
    ]
    assert not offenders, "embry0.mcp_server must stay a thin HTTP client of the orchestrator:\n" + "\n".join(offenders)
