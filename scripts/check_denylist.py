#!/usr/bin/env python3
"""Fail when a scanned file names something on an out-of-tree denylist.

The denylist holds the names that must never ship: customers, deployments,
people, hosts. It lives outside this repository on purpose, because a test
that spells out the names it guards publishes them. Its path comes from
EMBRY0_DENYLIST_FILE; CI and the public export write the file from a secret.
See docs/ops/verification.md, "Name denylist".

Each non-blank line of the denylist that does not start with `#` is a Python
regular expression, matched case-insensitively against every line of every
scanned file and against the file's path. `(?-i:...)` makes a term
case-sensitive.

An allowance masks one exact, case-sensitive literal before the patterns
run, in every file or in one named file, so a term may appear inside that
literal and nowhere else. The literal must not continue into a word on either
side: an allowed `github.com/owner/repo` does not cover
`github.com/owner/repo-other`. The public export uses this for the public
repository's own canonical URLs, which name its owner.

Exit codes: 0 clean, 1 a match, 2 the check did not run (nothing configured,
an unreadable or empty denylist, a scope path that is missing or holds no
file). A match is reported as the file, the line and the denylist line of the
pattern, never the matched text, so a CI log does not repeat what it guards.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "EMBRY0_DENYLIST_FILE"

# Paths scanned when none are given, relative to --root. They replace two
# in-tree guards that spelled out the names they protected: one checked
# docs/credentials.md, the other the API-key form AuthGate renders, whose text
# comes from AuthGate.tsx and the modules it imports. Widen this as the tree
# is cleaned; never narrow it.
DEFAULT_SCOPE: tuple[str, ...] = (
    "docs/credentials.md",
    "frontend/src/components/auth",
    "frontend/src/components/ui",
    "frontend/src/lib",
    "frontend/src/api",
    "frontend/src/stores",
)

EXIT_CLEAN = 0
EXIT_MATCH = 1
EXIT_NOT_RUN = 2

REPO_ROOT = Path(__file__).resolve().parents[1]


class NotRun(Exception):
    """The check could not run. Never reported as a pass."""


@dataclass(frozen=True)
class Pattern:
    denylist_line: int
    regex: re.Pattern[str]


@dataclass(frozen=True)
class Allowance:
    """An exact literal a term may appear inside, anywhere or only in `path`."""

    literal: str
    path: str | None = None

    def mask(self, line: str) -> str:
        """`line` with every bounded occurrence of the literal blanked out."""
        return _bounded(self.literal).sub(lambda m: " " * len(m.group()), line)


@functools.cache
def _bounded(literal: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(literal)}(?![\w-])")


@dataclass(frozen=True)
class Match:
    path: str
    line: int  # 0 means the file's path matched
    denylist_line: int


def load_patterns(denylist: Path) -> list[Pattern]:
    try:
        text = denylist.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise NotRun(f"cannot read the denylist {denylist}: {exc}") from None
    patterns = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            regex = re.compile(line, re.IGNORECASE)
        except re.error as exc:
            raise NotRun(f"denylist line {number} is not a valid regular expression ({exc.msg})") from None
        if regex.search(""):
            raise NotRun(f"denylist line {number} matches the empty string, so it would match every line")
        patterns.append(Pattern(number, regex))
    if not patterns:
        raise NotRun(f"the denylist {denylist} holds no patterns")
    return patterns


def _tracked_files(root: Path) -> set[str] | None:
    """Tracked files relative to root, or None when root is not in a git work tree."""
    try:
        inside = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached"],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        raise NotRun(f"git ls-files failed in {root}: {listed.stderr.strip()}")
    return {path for path in listed.stdout.split("\0") if path}


def _unreadable(error: OSError) -> None:
    # os.walk skips a directory it cannot list unless told otherwise, which
    # would scan less than asked and still report a pass.
    raise NotRun(f"cannot list {error.filename}: {error.strerror}")


def _files_under(root: Path, target: Path, tracked: set[str] | None) -> list[str]:
    rel = target.relative_to(root).as_posix()
    if tracked is not None:
        if target.is_file():
            return [rel] if rel in tracked else []
        prefix = "" if rel == "." else rel + "/"
        return [path for path in tracked if path.startswith(prefix) and (root / path).is_file()]
    if target.is_file():
        return [rel]
    found = []
    for directory, subdirs, names in os.walk(target, onerror=_unreadable):
        subdirs[:] = [name for name in subdirs if name != ".git"]
        for name in names:
            path = Path(directory, name)
            if path.is_file():
                found.append(path.relative_to(root).as_posix())
    return found


def scope_files(root: Path, scope: Sequence[str]) -> list[str]:
    """Files to scan, relative to root.

    In a git work tree only tracked files are scanned, which is what ships;
    anywhere else, such as an exported tree, every file is. A scope path that
    is missing or holds no file stops the check: a scan of nothing would look
    like a pass.
    """
    tracked = _tracked_files(root)
    files: set[str] = set()
    for entry in scope:
        target = (root / entry).resolve()
        if not target.is_relative_to(root):
            raise NotRun(f"scope path {entry} is outside {root}")
        if not target.exists():
            raise NotRun(f"scope path {entry} does not exist under {root}")
        found = _files_under(root, target, tracked)
        if not found:
            raise NotRun(f"scope path {entry} holds no file to scan")
        files.update(found)
    return sorted(files)


def scan(
    root: Path,
    files: Sequence[str],
    patterns: Sequence[Pattern],
    allowances: Sequence[Allowance] = (),
) -> list[Match]:
    matches = []
    for path in files:
        applicable = [a for a in allowances if a.path is None or a.path == path]
        for pattern in patterns:
            if pattern.regex.search(path):
                matches.append(Match(path, 0, pattern.denylist_line))
        try:
            text = (root / path).read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            raise NotRun(f"cannot read {path}: {exc}") from None
        for number, line in enumerate(text.split("\n"), start=1):
            for allowance in applicable:
                line = allowance.mask(line)
            for pattern in patterns:
                if pattern.regex.search(line):
                    matches.append(Match(path, number, pattern.denylist_line))
    return matches


def run(denylist_setting: str, root: Path, scope: Sequence[str], allowances: Sequence[Allowance] = ()) -> int:
    try:
        if not denylist_setting.strip():
            raise NotRun(f"{ENV_VAR} is not set, so no name was checked")
        denylist = Path(denylist_setting.strip()).expanduser().resolve()
        root = root.resolve()
        if denylist.is_relative_to(root):
            raise NotRun(f"the denylist {denylist} is inside the scanned tree; keep it outside the repository")
        patterns = load_patterns(denylist)
        files = scope_files(root, scope)
        for allowance in allowances:
            if not allowance.literal.strip():
                raise NotRun("an allowance has an empty literal")
            if allowance.path is not None and allowance.path not in files:
                raise NotRun(f"an allowance names {allowance.path}, which is not among the scanned files")
        matches = scan(root, files, patterns, allowances)
    except NotRun as exc:
        print(f"denylist check NOT RUN: {exc}", file=sys.stderr)
        print("This is not a pass: nothing was checked.", file=sys.stderr)
        return EXIT_NOT_RUN
    for match in matches:
        where = f"{match.path}:{match.line}" if match.line else f"{match.path} (its path)"
        print(f"{where}: matches denylist line {match.denylist_line}")
    summary = f"{len(patterns)} patterns, {len(files)} files"
    if allowances:
        summary += f", {len(allowances)} allowances"
    if matches:
        hit_files = len({match.path for match in matches})
        print(f"denylist check FAILED: {len(matches)} matches in {hit_files} files ({summary})")
        return EXIT_MATCH
    print(f"denylist check passed: 0 matches ({summary})")
    return EXIT_CLEAN


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Fail when a file names a term from the denylist file named by {ENV_VAR}.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="files or directories relative to --root; default: the guarded paths in DEFAULT_SCOPE",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="the tree to scan; default: this repository",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="LITERAL",
        help="an exact literal a term may appear inside, in any file (repeatable)",
    )
    parser.add_argument(
        "--allow-in",
        action="append",
        nargs=2,
        default=[],
        metavar=("PATH", "LITERAL"),
        help="an exact literal a term may appear inside, only in PATH relative to --root (repeatable)",
    )
    args = parser.parse_args(argv)
    allowances = [Allowance(literal) for literal in args.allow]
    allowances += [Allowance(literal, path) for path, literal in args.allow_in]
    return run(os.environ.get(ENV_VAR, ""), args.root, args.paths or DEFAULT_SCOPE, allowances)


if __name__ == "__main__":
    sys.exit(main())
