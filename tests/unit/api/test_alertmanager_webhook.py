"""POST /api/v1/webhook/alertmanager, and CI failures as cases (#296, SH-P3.8).

Detection was one regex over one stack's logs. Prometheus, Alertmanager, Loki
and cadvisor all ran on this host already and none of them could open a case —
Alertmanager's only receivers were Discord and a WhatsApp bridge, neither of
which the operator reads. These tests pin that a firing alert becomes a case
readable over MCP, and that every source dedupes through the same ledger.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config
from embry0.services.healing.lifecycle import Origin
from tests.fixtures.webhooks import alertmanager_alert, github_workflow_run

REPO_MAP = json.dumps({"acme-backend": "acme/backend"})


class _FakeCases:
    """Enough of HealingCaseService for the receiver's three branches."""

    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        self.existing = existing
        self.opened: list[dict[str, Any]] = []
        self.recurrences: list[tuple[str, Any]] = []
        self.closed: list[tuple[str, Any]] = []

    async def find_by_fingerprint(self, fingerprint: str, *, origin: Any, repo: str | None = None):
        self.last_lookup = {"fingerprint": fingerprint, "origin": origin, "repo": repo}
        return self.existing

    async def open_case(self, **kwargs: Any):
        # The REAL return type. The first version of this fake returned an
        # object with a `.case` attribute — the same wrong assumption the
        # handler made — so every case_id came back null and this passed.
        from embry0.services.healing.service import CaseResult, WriteOutcome

        self.opened.append(kwargs)
        return CaseResult({"id": f"hc-{len(self.opened)}"}, WriteOutcome.APPLIED)

    async def record_recurrence(self, case_id: str, **kwargs: Any):
        self.recurrences.append((case_id, kwargs.get("event_key")))
        return None

    async def close(self, case_id: str, **kwargs: Any):
        self.closed.append((case_id, kwargs.get("resolution_path")))
        return None


def _app(*, secret: str = "", dev: bool = True, cases: _FakeCases | None = None, repo_map: str = REPO_MAP):
    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    config = Embry0Config(
        _env_file=None,
        database_url="postgresql://unused",
        auth_dev_mode=True,
        webhook_dev_mode=dev,
        alertmanager_webhook_secret=secret,
        alertmanager_repo_map=repo_map,
        github_webhook_secret="",
        watcher_repo="",
    )
    app = create_app(config, lifespan_override=_noop_lifespan)
    app.state.config = config
    app.state.issues_repo = object()
    app.state.issue_executor = object()
    app.state.healing_cases = cases if cases is not None else _FakeCases()
    return app, app.state.healing_cases


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _post(app, payload, headers=None):
    async with _client(app) as client:
        return await client.post("/api/v1/webhook/alertmanager", json=payload, headers=headers or {})


# ---- authentication: the same policy as every other receiver ---------------


async def test_the_bearer_token_is_compared_verbatim() -> None:
    app, _ = _app(secret="s3cret", dev=False)
    ok = await _post(app, alertmanager_alert(), {"Authorization": "Bearer s3cret"})
    bad = await _post(app, alertmanager_alert(), {"Authorization": "Bearer nope"})
    assert ok.status_code == 200, ok.text
    assert bad.status_code == 401


async def test_missing_token_is_401_when_a_secret_is_configured() -> None:
    app, _ = _app(secret="s3cret", dev=False)
    assert (await _post(app, alertmanager_alert())).status_code == 401


async def test_unconfigured_secret_outside_dev_mode_is_503() -> None:
    """A webhook that silently accepts anything is worse than one that refuses."""
    app, _ = _app(secret="", dev=False)
    assert (await _post(app, alertmanager_alert())).status_code == 503


async def test_dev_mode_bypasses_only_an_empty_secret() -> None:
    app, _ = _app(secret="", dev=True)
    assert (await _post(app, alertmanager_alert())).status_code == 200

    app, _ = _app(secret="s3cret", dev=True)
    r = await _post(app, alertmanager_alert(), {"Authorization": "Bearer wrong"})
    assert r.status_code == 401, "a configured secret is enforced even in dev mode"


async def test_invalid_json_is_400() -> None:
    app, _ = _app()
    async with _client(app) as client:
        r = await client.post(
            "/api/v1/webhook/alertmanager",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 400


# ---- a firing alert becomes a case -----------------------------------------


async def test_a_firing_alert_opens_a_case_with_origin_alert() -> None:
    app, cases = _app()
    r = await _post(app, alertmanager_alert())
    assert r.status_code == 200
    assert r.json()["recorded"] == 1
    assert len(cases.opened) == 1
    opened = cases.opened[0]
    assert opened["repo"] == "acme/backend"
    assert opened["origin"] is Origin.ALERT
    assert "ContainerKilled" in opened["title"]
    assert opened["severity"] == "critical"
    assert opened["fingerprint"] == "9f2b1c4e7a8d0e13", "the alert's own fingerprint is the dedup key"


async def test_a_repeated_firing_alert_counts_a_recurrence_instead_of_filing_again() -> None:
    """Alertmanager resends every repeat_interval — 4h on this deployment."""
    app, cases = _app(cases=_FakeCases(existing={"id": "hc-9", "stage": "detected"}))
    r = await _post(app, alertmanager_alert())
    assert r.status_code == 200
    assert cases.recurrences == [("hc-9", "alert:9f2b1c4e7a8d0e13:2026-09-21T04:00:00+00:00:firing")]
    assert not cases.opened


async def test_a_resolved_alert_closes_the_case_as_self_resolved() -> None:
    """Nothing fixed it; it stopped. That is what self_resolved means."""
    app, cases = _app(cases=_FakeCases(existing={"id": "hc-9", "stage": "detected"}))
    r = await _post(app, alertmanager_alert(status="resolved"))
    assert r.status_code == 200
    assert [c[0] for c in cases.closed] == ["hc-9"]
    assert str(cases.closed[0][1]) == "self_resolved"


async def test_a_resolved_alert_with_no_open_case_does_nothing() -> None:
    app, cases = _app()
    r = await _post(app, alertmanager_alert(status="resolved"))
    assert r.status_code == 200
    assert not cases.opened and not cases.closed and not cases.recurrences


async def test_a_firing_alert_whose_case_was_closed_links_to_it() -> None:
    app, cases = _app(cases=_FakeCases(existing={"id": "hc-old", "stage": "closed"}))
    await _post(app, alertmanager_alert())
    assert len(cases.opened) == 1
    assert cases.opened[0]["parent_case_id"] == "hc-old", "a regression lost its link to the closed case"


# ---- repo attribution ------------------------------------------------------


async def test_an_alert_with_no_repo_mapping_is_skipped_not_guessed() -> None:
    """`healing_cases.repo` is NOT NULL and guessing files against the wrong
    repo, which is worse than not filing."""
    app, cases = _app(repo_map="{}")
    r = await _post(app, alertmanager_alert(labels={"name": "something-unmapped"}))
    assert r.status_code == 200
    assert not cases.opened
    assert r.json()["skipped"], "a dropped alert must say why"


async def test_an_explicit_repo_label_wins_over_the_map() -> None:
    app, cases = _app()
    await _post(app, alertmanager_alert(labels={"name": "acme-backend", "repo": "owner/explicit"}))
    assert cases.opened[0]["repo"] == "owner/explicit"


async def test_an_unparseable_repo_map_maps_nothing_rather_than_crashing() -> None:
    """A typo in one env var must not 500 at Alertmanager."""
    app, cases = _app(repo_map="{not json")
    r = await _post(app, alertmanager_alert())
    assert r.status_code == 200
    assert not cases.opened


async def test_the_dedupe_lookup_is_scoped_to_the_alert_origin() -> None:
    """So an alert fingerprint can never collide with a watcher one."""
    app, cases = _app()
    await _post(app, alertmanager_alert())
    assert cases.last_lookup["origin"] is Origin.ALERT
    assert cases.last_lookup["repo"] == "acme/backend"


# ---- CI on main ------------------------------------------------------------


async def _post_github(app, payload, event="workflow_run"):
    async with _client(app) as client:
        return await client.post(
            "/api/v1/webhook",
            json=payload,
            headers={"X-GitHub-Event": event, "X-GitHub-Delivery": "d-1"},
        )


async def test_a_failed_workflow_run_on_main_opens_a_ci_case() -> None:
    app, cases = _app()
    r = await _post_github(app, github_workflow_run())
    assert r.status_code == 200, r.text
    assert len(cases.opened) == 1
    assert cases.opened[0]["origin"] is Origin.CI
    assert "CI failing on main" in cases.opened[0]["title"]
    assert cases.opened[0]["repo"] == "acme/backend"


async def test_a_failed_run_on_a_feature_branch_is_ignored() -> None:
    """A red feature branch is the pipeline working; #305 handles those."""
    app, cases = _app()
    await _post_github(app, github_workflow_run(branch="alex/some-feature"))
    assert not cases.opened


async def test_a_successful_run_is_not_a_case() -> None:
    app, cases = _app()
    await _post_github(app, github_workflow_run(conclusion="success"))
    assert not cases.opened and not cases.closed


async def test_the_same_workflow_failing_again_is_one_case() -> None:
    """Keyed on repo+workflow+branch, not the run id: every failing attempt of
    the same build is the same broken build."""
    app, cases = _app(cases=_FakeCases(existing={"id": "hc-ci", "stage": "detected"}))
    await _post_github(app, github_workflow_run(run_id=1))
    await _post_github(app, github_workflow_run(run_id=2))
    assert not cases.opened
    assert len(cases.recurrences) == 2


async def test_the_workflow_going_green_closes_the_case() -> None:
    app, cases = _app(cases=_FakeCases(existing={"id": "hc-ci", "stage": "detected"}))
    await _post_github(app, github_workflow_run(conclusion="success"))
    assert [c[0] for c in cases.closed] == ["hc-ci"]


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
async def test_every_failing_conclusion_counts(conclusion: str) -> None:
    app, cases = _app()
    await _post_github(app, github_workflow_run(conclusion=conclusion))
    assert len(cases.opened) == 1, f"{conclusion} did not open a case"


# ---- wiring ----------------------------------------------------------------


async def test_an_unwired_ledger_reports_so_rather_than_500() -> None:
    app, _ = _app()
    app.state.healing_cases = None
    r = await _post(app, alertmanager_alert())
    assert r.status_code == 200
    assert r.json()["recorded"] == 0


async def test_record_signal_returns_the_real_case_id() -> None:
    from types import SimpleNamespace

    from embry0.api.v1.webhooks import _record_signal
    from embry0.services.healing.signals import signals_from_alertmanager

    cases = _FakeCases()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(healing_cases=cases)))
    (signal,), _ = signals_from_alertmanager(alertmanager_alert(), repo_map={"acme-backend": "o/r"}, default_repo=None)
    out = await _record_signal(request, signal)
    assert out["case_id"] == "hc-1", f"case_id came back {out['case_id']!r}"
