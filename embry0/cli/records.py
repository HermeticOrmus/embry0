"""``embry0 records`` — a thin client of the repo records API, plus a one-time importer.

The store lives in the orchestrator (docs/records.md); every subcommand here
is an HTTP call to it, authenticated like ``embry0 onboard``.

``import`` walks a directory of Markdown files and writes one record per file.
The first matching directory prefix picks the kind (``IMPORT_KINDS``),
anything else is a ``note``, and ``README.md`` files are skipped: in such a
directory they describe the directory, not a document. The slug is the file
name. Writing an unchanged file again adds no revision, so a re-run is safe.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote

import httpx

# Directory prefix (relative to the import root) -> kind. Most specific first.
IMPORT_KINDS: tuple[tuple[str, str], ...] = (
    ("superpowers/specs/", "spec"),
    ("superpowers/plans/", "plan"),
    ("audits/", "audit"),
    ("rca/", "rca"),
    ("spikes/", "spike"),
    ("ops/", "runbook"),
)
DEFAULT_KIND = "note"
_SLUG_MAX = 128


class RecordsCliError(Exception):
    """The API refused or could not be reached; the message is for the operator."""


def kind_for(rel_path: str) -> str:
    for prefix, kind in IMPORT_KINDS:
        if rel_path.startswith(prefix):
            return kind
    return DEFAULT_KIND


def slug_for(filename: str) -> str:
    """``2026-10-01 Cache Outage.md`` -> ``2026-10-01-cache-outage``; "" when nothing usable is left."""
    stem = filename[:-3] if filename.lower().endswith(".md") else filename
    slug = re.sub(r"[^a-z0-9._-]+", "-", stem.lower())
    slug = re.sub(r"^[^a-z0-9]+", "", slug)[:_SLUG_MAX]
    return slug


@dataclass(frozen=True)
class ImportItem:
    path: Path
    rel: str
    slug: str
    kind: str


@dataclass(frozen=True)
class ImportProblem:
    rel: str
    reason: str


def plan_import(root: Path) -> tuple[list[ImportItem], list[ImportProblem]]:
    """What an import of ``root`` would write, and the files it cannot write."""
    items: list[ImportItem] = []
    problems: list[ImportProblem] = []
    by_slug: dict[str, list[ImportItem]] = {}
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if path.name.lower() == "readme.md":
            continue
        slug = slug_for(path.name)
        if not slug:
            problems.append(ImportProblem(rel, "no usable slug in the file name"))
            continue
        item = ImportItem(path, rel, slug, kind_for(rel))
        by_slug.setdefault(slug, []).append(item)
    for slug, group in by_slug.items():
        if len(group) > 1:
            others = ", ".join(i.rel for i in group)
            problems.extend(ImportProblem(i.rel, f"slug {slug!r} is shared by {others}") for i in group)
        else:
            items.append(group[0])
    items.sort(key=lambda i: i.rel)
    problems.sort(key=lambda p: p.rel)
    return items, problems


class RecordsApi:
    """The records routes of one orchestrator."""

    def __init__(self, base_url: str, api_key: str | None, *, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
        )

    def _path(self, repo: str, slug: str = "") -> str:
        base = f"/api/v1/repos/{repo.strip('/')}/records"
        return f"{base}/{quote(slug, safe='')}" if slug else base

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RecordsCliError(f"cannot reach the orchestrator at {self.base_url}: {exc}") from exc
        if resp.status_code == 401:
            raise RecordsCliError("orchestrator rejected the request (401): set EMBRY0_API_KEY or pass --api-key")
        if resp.status_code >= 400:
            detail: Any = resp.text[:500]
            try:
                detail = resp.json().get("detail", detail)
            except ValueError:
                pass
            raise RecordsCliError(f"{method} {path} failed ({resp.status_code}): {detail}")
        return resp.json() if resp.content else None

    def list_records(self, repo: str, *, kind: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        params = {k: v for k, v in {"kind": kind, "q": query}.items() if v}
        return list(self._call("GET", self._path(repo), params=params)["records"])

    def get(self, repo: str, slug: str, *, revision: int | None = None) -> dict[str, Any]:
        params = {"revision": revision} if revision else None
        return dict(self._call("GET", self._path(repo, slug), params=params))

    def put(self, repo: str, slug: str, *, kind: str, body: str, title: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": kind, "body": body}
        if title:
            payload["title"] = title
        return dict(self._call("PUT", self._path(repo, slug), json=payload))


def run_import(api: RecordsApi, root: Path, repo: str, *, dry_run: bool = False, out: TextIO = sys.stdout) -> int:
    """Import every Markdown file under ``root``. Returns the exit code: 1 when any file failed."""
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=out)
        return 2
    items, problems = plan_import(root)
    failed = bool(problems)
    for problem in problems:
        print(f"skipped   {problem.rel}: {problem.reason}", file=out)
    counts: dict[str, int] = {}
    for item in items:
        if dry_run:
            outcome = "would-put"
            print(f"{outcome:<9} {item.rel} -> {item.kind}/{item.slug}", file=out)
        else:
            try:
                body = item.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                failed = True
                print(f"skipped   {item.rel}: unreadable ({exc})", file=out)
                continue
            try:
                result = api.put(repo, item.slug, kind=item.kind, body=body)
            except RecordsCliError as exc:
                failed = True
                outcome = "conflict" if "(409)" in str(exc) else "failed"
                print(f"{outcome:<9} {item.rel}: {exc}", file=out)
                counts[outcome] = counts.get(outcome, 0) + 1
                continue
            outcome = "created" if result.get("created") else ("updated" if result.get("changed") else "unchanged")
            print(f"{outcome:<9} {item.rel} -> {item.kind}/{item.slug} (revision {result.get('revision')})", file=out)
        counts[outcome] = counts.get(outcome, 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "nothing to import"
    print(f"{repo}: {summary}; {len(problems)} skipped", file=out)
    return 1 if failed else 0


def run(args: Any, api: RecordsApi, out: TextIO = sys.stdout) -> int:
    """Dispatch one ``embry0 records`` subcommand. Returns the exit code."""
    try:
        if args.action == "import":
            return run_import(api, Path(args.directory), args.repo, dry_run=args.dry_run, out=out)
        if args.action == "list":
            rows = api.list_records(args.repo, kind=args.kind, query=args.query)
            if args.json:
                print(json.dumps(rows, indent=2, default=str), file=out)
            for row in [] if args.json else rows:
                print(f"{row['kind']:<8} {row['slug']:<40} r{row['current_revision']:<3} {row['title']}", file=out)
            return 0
        if args.action == "get":
            record = api.get(args.repo, args.slug, revision=args.revision)
            print(json.dumps(record, indent=2, default=str) if args.json else record["body"], file=out)
            return 0
        if args.action == "put":
            body = Path(args.file).read_text(encoding="utf-8") if args.file != "-" else sys.stdin.read()
            result = api.put(args.repo, args.slug, kind=args.kind, body=body, title=args.title)
            state = "created" if result["created"] else ("updated" if result["changed"] else "unchanged")
            print(f"{state} {args.repo} {args.slug} (revision {result['current_revision']})", file=out)
            return 0
    except RecordsCliError as exc:
        print(f"error: {exc}", file=out)
        return 1
    raise ValueError(f"unknown records action {args.action!r}")
