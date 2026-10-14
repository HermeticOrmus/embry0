"""``embry0 records``: the importer's mapping and idempotency, against a fake API.

The fake keeps records the way the real store does (kind fixed at creation,
no revision for an identical body), so a re-run of the import is measured,
not assumed.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from embry0.cli.records import RecordsApi, kind_for, plan_import, run, run_import, slug_for

REPO = "group/sub/project"


class FakeStore:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        prefix = f"/api/v1/repos/{REPO}/records"
        path = request.url.path
        if not path.startswith(prefix):
            return httpx.Response(404, json={"detail": "repo not registered"})
        slug = path[len(prefix) + 1 :]
        if request.method == "GET" and not slug:
            rows = [{"slug": s, **r} for s, r in self.records.items()]
            return httpx.Response(200, json={"repo": REPO, "records": rows})
        if request.method == "GET":
            rec = self.records.get(slug)
            return httpx.Response(200, json={"slug": slug, **rec}) if rec else httpx.Response(404, json={})
        assert request.method == "PUT"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        payload = json.loads(request.content)
        rec = self.records.get(slug)
        if rec is None:
            self.records[slug] = {**payload, "current_revision": 1, "title": payload.get("title") or slug}
            return httpx.Response(201, json={"created": True, "changed": True, "revision": 1, "current_revision": 1})
        if rec["kind"] != payload["kind"]:
            return httpx.Response(409, json={"detail": "the kind of a record is fixed when it is created"})
        changed = rec["body"] != payload["body"]
        if changed:
            rec["body"] = payload["body"]
            rec["current_revision"] += 1
        n = rec["current_revision"]
        return httpx.Response(200, json={"created": False, "changed": changed, "revision": n, "current_revision": n})

    def api(self) -> RecordsApi:
        return RecordsApi("http://embry0.test", "key", transport=httpx.MockTransport(self.handler))


def _tree(root: Path) -> Path:
    files = {
        "audits/2026-09 Security Audit.md": "# Security audit\n",
        "rca/cache-outage.md": "# Cache outage\n",
        "spikes/vector-search.md": "# Spike\n",
        "ops/restart.md": "# Restart\n",
        "superpowers/specs/2026-10-01-feature-design.md": "# Design\n",
        "superpowers/plans/2026-10-01-feature.md": "# Plan\n",
        "superpowers/other.md": "# Other\n",
        "loose.md": "# Loose\n",
        "README.md": "# What goes here\n",
        "rca/README.md": "# RCA index\n",
        "notes.txt": "not markdown",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_kind_mapping() -> None:
    assert kind_for("audits/a.md") == "audit"
    assert kind_for("rca/a.md") == "rca"
    assert kind_for("spikes/a.md") == "spike"
    assert kind_for("ops/a.md") == "runbook"
    assert kind_for("superpowers/specs/a.md") == "spec"
    assert kind_for("superpowers/plans/a.md") == "plan"
    assert kind_for("superpowers/a.md") == "note"
    assert kind_for("nested/rca/a.md") == "note", "only a top-level directory picks the kind"


def test_slug_rules() -> None:
    assert slug_for("2026-09 Security Audit.md") == "2026-09-security-audit"
    assert slug_for("_draft.md") == "draft"
    assert slug_for("Ünïcode.md") == "n-code"
    assert slug_for("!!!.md") == ""
    assert len(slug_for("a" * 300 + ".md")) == 128


def test_plan_skips_readmes_and_non_markdown(tmp_path: Path) -> None:
    items, problems = plan_import(_tree(tmp_path))
    assert problems == []
    assert {(i.rel, i.kind, i.slug) for i in items} == {
        ("audits/2026-09 Security Audit.md", "audit", "2026-09-security-audit"),
        ("rca/cache-outage.md", "rca", "cache-outage"),
        ("spikes/vector-search.md", "spike", "vector-search"),
        ("ops/restart.md", "runbook", "restart"),
        ("superpowers/specs/2026-10-01-feature-design.md", "spec", "2026-10-01-feature-design"),
        ("superpowers/plans/2026-10-01-feature.md", "plan", "2026-10-01-feature"),
        ("superpowers/other.md", "note", "other"),
        ("loose.md", "note", "loose"),
    }


def test_two_files_with_one_slug_are_both_refused(tmp_path: Path) -> None:
    (tmp_path / "rca").mkdir()
    (tmp_path / "ops").mkdir()
    (tmp_path / "rca" / "outage.md").write_text("a")
    (tmp_path / "ops" / "Outage.md").write_text("b")
    items, problems = plan_import(tmp_path)
    assert items == [], "neither file may silently win the slug"
    assert {p.rel for p in problems} == {"rca/outage.md", "ops/Outage.md"}


def test_import_is_idempotent(tmp_path: Path) -> None:
    store = FakeStore()
    root = _tree(tmp_path)
    first = io.StringIO()
    assert run_import(store.api(), root, REPO, out=first) == 0, first.getvalue()
    assert len(store.records) == 8
    assert store.records["cache-outage"]["kind"] == "rca"
    assert "8 created" in first.getvalue()

    second = io.StringIO()
    assert run_import(store.api(), root, REPO, out=second) == 0, second.getvalue()
    assert "8 unchanged" in second.getvalue(), second.getvalue()
    assert all(r["current_revision"] == 1 for r in store.records.values()), "a re-run must add no revision"

    (root / "rca" / "cache-outage.md").write_text("# Cache outage\n\nfollow-up\n")
    third = io.StringIO()
    assert run_import(store.api(), root, REPO, out=third) == 0
    assert "1 updated" in third.getvalue() and "7 unchanged" in third.getvalue()
    assert store.records["cache-outage"]["current_revision"] == 2


def test_import_reports_a_kind_conflict_and_fails(tmp_path: Path) -> None:
    store = FakeStore()
    store.records["restart"] = {"kind": "note", "body": "x", "current_revision": 1, "title": "x"}
    out = io.StringIO()
    assert run_import(store.api(), _tree(tmp_path), REPO, out=out) == 1
    assert "conflict  ops/restart.md" in out.getvalue()
    assert store.records["restart"]["kind"] == "note"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    store = FakeStore()
    out = io.StringIO()
    assert run_import(store.api(), _tree(tmp_path), REPO, dry_run=True, out=out) == 0
    assert store.requests == []
    assert "would-put rca/cache-outage.md -> rca/cache-outage" in out.getvalue()


def test_list_get_put_subcommands(tmp_path: Path) -> None:
    store = FakeStore()
    api = store.api()
    doc = tmp_path / "doc.md"
    doc.write_text("# Doc\n")
    put = SimpleNamespace(action="put", repo=REPO, slug="doc", kind="note", file=str(doc), title=None)
    out = io.StringIO()
    assert run(put, api, out) == 0
    assert out.getvalue().startswith("created group/sub/project doc")
    out = io.StringIO()
    assert run(SimpleNamespace(action="get", repo=REPO, slug="doc", revision=None, json=False), api, out) == 0
    assert out.getvalue() == "# Doc\n\n"
    out = io.StringIO()
    assert run(SimpleNamespace(action="list", repo=REPO, kind=None, query=None, json=False), api, out) == 0
    assert "doc" in out.getvalue()
    out = io.StringIO()
    assert run(SimpleNamespace(action="get", repo="acme/ghost", slug="x", revision=None, json=False), api, out) == 1
    assert "404" in out.getvalue()
