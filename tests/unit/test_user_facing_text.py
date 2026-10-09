"""Text an operator reads names no tracker ticket (#528).

A ticket key such as ``ABC-123`` in a code comment or an internal docstring
is provenance, and the project keeps it there. In text that reaches an
operator it points at one deployment's tracker, which nobody else can open:

- OpenAPI, which is built from route docstrings and schema docstrings;
- MCP tool descriptions, which are tool docstrings;
- runtime strings: CLI help and output, error messages, generated files,
  seeded rows (pipeline template descriptions, agent prompts);
- agent prompt files;
- the Auth0 role descriptions ``provision-roles.mjs`` writes.

Migrations are exempt: their descriptions and ``COMMENT ON`` text are a
record of what each one did. The dashboard's own text is guarded by
``frontend/src/__tests__/userFacingText.test.ts``.

A failure names the text. Move the ticket into a comment beside it.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from embry0.api.app import create_app
from embry0.config import Embry0Config
from embry0_mcp import server

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (ROOT / "embry0", ROOT / "embry0_mcp")
MIGRATIONS = ROOT / "embry0" / "storage" / "migrations"

TICKET_REF = re.compile(r"\b([A-Z]{2,}[A-Z0-9]*)-\d+\b")
# Words of that shape that are not tickets. ENG is the example key the
# docs and types use.
NOT_TICKETS = frozenset({"AES", "CVE", "ENG", "ISO", "PEP", "RFC", "SHA", "UTF"})


def ticket_refs(text: str) -> list[str]:
    return [m.group(0) for m in TICKET_REF.finditer(text) if m.group(1) not in NOT_TICKETS]


def _strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _strings(value, f"{path}/{i}")
    elif isinstance(node, str):
        yield path, node


def _hits(texts: Iterator[tuple[str, str]]) -> list[str]:
    return [f"{where}: {ref}" for where, text in texts for ref in ticket_refs(text)]


def _runtime_strings(path: Path, root: Path = ROOT) -> Iterator[tuple[str, str]]:
    """Every string literal in a module except docstrings and bare string statements."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    statements = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in statements:
            yield f"{path.relative_to(root)}:{node.lineno}", node.value


def test_the_detector_tells_a_ticket_from_a_lookalike() -> None:
    assert ticket_refs("shipped in ABC-12 and XY-3") == ["ABC-12", "XY-3"]
    assert ticket_refs("UTF-8, SHA-256, RFC-8628, ENG-123, A-1, Z0-9, abc-12") == []


def test_the_runtime_scan_skips_docstrings_and_keeps_literals(tmp_path: Path) -> None:
    module = tmp_path / "m.py"
    module.write_text('"""Doc ABC-1."""\n\ndef f():\n    """Doc ABC-2."""\n    return "Text ABC-3"\n')
    found = [text for _, text in _runtime_strings(module, tmp_path)]
    assert found == ["Text ABC-3"]


def test_openapi_names_no_ticket() -> None:
    app = create_app(Embry0Config(_env_file=None, auth_dev_mode=True, webhook_dev_mode=True))
    spec = app.openapi()
    assert len(spec["paths"]) > 50, "the app served almost no routes; the scan would prove nothing"
    hits = _hits(_strings(spec))
    assert hits == [], f"OpenAPI text names a ticket (route or schema docstring): {hits}"


async def test_mcp_tools_name_no_ticket() -> None:
    tools = await server.mcp.list_tools()
    assert len(tools) > 20, "the server listed almost no tools; the scan would prove nothing"
    wire = {t.name: t.model_dump(mode="json", exclude_none=True) for t in tools}
    hits = _hits(_strings({"instructions": server.mcp.instructions, "tools": wire}))
    assert hits == [], f"an MCP tool description names a ticket: {hits}"


def test_runtime_strings_name_no_ticket() -> None:
    modules = [
        path
        for package in PACKAGES
        for path in sorted(package.rglob("*.py"))
        if MIGRATIONS not in path.parents and "__pycache__" not in path.parts
    ]
    assert len(modules) > 200, "almost no modules found; the scan would prove nothing"
    hits = [hit for path in modules for hit in _hits(_runtime_strings(path))]
    assert hits == [], f"a runtime string names a ticket: {hits}"


def test_agent_prompt_files_name_no_ticket() -> None:
    prompts = sorted((ROOT / "embry0").rglob("*.md"))
    assert prompts, "no prompt files found; the scan would prove nothing"
    hits = [
        f"{path.relative_to(ROOT)}: {ref}" for path in prompts for ref in ticket_refs(path.read_text(encoding="utf-8"))
    ]
    assert hits == [], f"an agent prompt names a ticket: {hits}"


def test_auth0_role_descriptions_name_no_ticket() -> None:
    script = (ROOT / "infra" / "auth0" / "provision-roles.mjs").read_text(encoding="utf-8")
    descriptions = re.findall(r"description:\s*'([^']*)'", script)
    assert len(descriptions) == 2, f"expected the two role descriptions, found {descriptions}"
    hits = [ref for text in descriptions for ref in ticket_refs(text)]
    assert hits == [], f"an Auth0 role description names a ticket: {hits}"
