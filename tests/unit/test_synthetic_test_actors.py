"""#533: tests name a synthetic actor, never the project owner.

Who approves, dispatches or closes something in a test is a made-up person
(`alex`). The owner's first name kept appearing there instead, and a test
fixture is the example the next twenty tests copy. Attribution stays where it
belongs: LICENSE, pyproject and the community files, none of which this scans.

The name is read from LICENSE rather than written here, because a test that
spells out the name it guards against publishes it (CONTRIBUTING.md,
"Vendor-neutral naming"). It is compared case- and accent-insensitively as a
whole word, so a forge handle that merely contains it is not a match.
"""

import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LICENSE = ROOT / "LICENSE"
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".json", ".yaml", ".yml", ".md", ".txt", ".sql", ".toml"}
SKIP_DIRS = {"__pycache__", "node_modules", ".pytest_cache"}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _owner_first_name() -> str:
    match = re.search(r"^Copyright \(c\) \d{4}(?:-\d{4})? (\S+) ", LICENSE.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"no 'Copyright (c) <year> <name>' line in {LICENSE}; the guard cannot run"
    return _fold(match.group(1))


def _matcher(name: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(name)}\b")


def _test_files() -> list[Path]:
    def text_files(base: Path) -> list[Path]:
        return [
            path
            for path in base.rglob("*")
            if path.is_file() and path.suffix in TEXT_SUFFIXES and not SKIP_DIRS.intersection(path.parts)
        ]

    frontend = [
        path
        for path in text_files(ROOT / "frontend" / "src")
        if "__tests__" in path.parts or path.name.endswith((".test.ts", ".test.tsx"))
    ]
    return text_files(ROOT / "tests") + frontend


def test_the_matcher_catches_the_owner_name_in_every_spelling() -> None:
    name = _owner_first_name()
    matcher = _matcher(name)
    accented = name[:-2] + name[-2] + "́" + name[-1]
    for spelling in (name, name.capitalize(), name.upper(), accented.capitalize()):
        assert matcher.search(_fold(f'decided_by="{spelling}"')), f"missed a spelling of the owner's name: {spelling!r}"
    assert matcher.search(_fold(f"{name}@example.com")), "missed the owner's name as an email local part"
    assert not matcher.search(_fold(f"https://github.com/someone{name.capitalize()}/repo")), (
        "a handle that only contains the name was flagged"
    )


def test_no_test_file_names_the_owner_as_an_actor() -> None:
    matcher = _matcher(_owner_first_name())
    files = _test_files()
    assert len(files) > 500, f"scanned only {len(files)} test files; the guard is looking in the wrong place"
    assert Path(__file__) in files, "the guard does not scan its own directory"
    assert any(path.suffix == ".tsx" for path in files), "no frontend test files were scanned"

    hits = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in files
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1)
        if matcher.search(_fold(line))
    ]
    assert not hits, f"use a synthetic actor such as 'alex' instead of the owner's name: {hits}"
