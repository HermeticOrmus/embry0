"""Files the public export ships do not refer to a path it drops.

The export (scripts/export_public.py) removes every path in its DROP list:
internal/ and any single file the owner keeps private. A link or path in a
shipped file that points at one of them dangles in the public repository, and
names material that is not there. The export runs the same check over the
exported tree and stops on a hit; this test catches it in review instead.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "export_public.py"


def _load_export() -> ModuleType:
    assert SCRIPT.is_file(), f"the export script {SCRIPT} is missing"
    spec = importlib.util.spec_from_file_location("export_public", SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _shipped_files() -> list[str]:
    """Tracked files in a git checkout; every file outside .git otherwise (an exported tree)."""
    if (ROOT / ".git").exists():
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True)
        return [rel for rel in out.stdout.split("\0") if rel and (ROOT / rel).is_file()]
    return [
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("*")
        if p.is_file() and not {".git", ".venv", "node_modules"}.intersection(p.relative_to(ROOT).parts)
    ]


def test_no_shipped_file_refers_to_a_dropped_path():
    export = _load_export()
    found = export.dropped_references(ROOT, _shipped_files())
    assert not found, "shipped files refer to paths the public export drops (scripts/export_public.py DROP):\n" + (
        "\n".join(found)
    )


def test_the_reference_check_reports_what_would_dangle(tmp_path):
    """The check above passes only if it can fail."""
    export = _load_export()
    drops = (export.Drop("private/", "test"), export.Drop("docs/secret-plan.md", "test"))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(
        "Put specs under `private/`.\n"  # names the directory: allowed
        "See [the spec](../private/specs/x.md).\n"  # a link into it, relative to the file
        "Spec: `private/specs/y.md`.\n"  # a file under it, relative to the root
        "[folder](../private/specs/)\n"  # a Markdown link to a directory under it
        "Roadmap: [plan](secret-plan.md)\n"  # the dropped file, relative to the file
        "POST /api/v1/private/presign and https://example.com/private/a.md\n"  # a route and a URL: not paths
    )
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "README.md").write_text("[back](../docs/secret-plan.md)\n")  # dropped itself
    files = ["docs/guide.md", "private/README.md"]
    assert export.dropped_references(tmp_path, files, drops, exempt=()) == [
        "docs/guide.md:2: private/specs/x.md",
        "docs/guide.md:3: private/specs/y.md",
        "docs/guide.md:4: private/specs",
        "docs/guide.md:5: docs/secret-plan.md",
    ]
