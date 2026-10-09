"""POST /api/v1/cases/{id}/accept for a Linear draft (#460).

Accepting puts the trigger label on the Linear ticket for the person and
dispatches through the Linear trigger path. Everything embry0-side is real —
the route, ``LinearSyncService``, the executor's dispatch, the case ledger,
the issue and job rows on Postgres. Linear is a fake GraphQL server behind
the service's own HTTP client, so the mutations sent are the real ones and
no request leaves the process.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from embry0.orchestration.budget_gate import DispatchDecision
from embry0.services import linear_sync
from embry0.services.healing.service import HealingCaseService, TrackerRef
from embry0.services.linear_sync import LinearSyncService
from embry0.services.watcher_stacks import WatcherStack
from embry0.storage.repositories.watcher_stacks import WatcherStacksRepository

REPO = "acme/backend"
KEY = "ENG-1"
NATIVE_ID = "11111111-aaaa-4bbb-8ccc-000000000001"
TEAM_ID = "team-eng"
LABEL_ID = "lbl-trigger"
URL = f"https://linear.app/acme/issue/{KEY}"


class FakeLinear:
    """One Linear ticket and the workspace's labels, served over GraphQL.

    ``ops`` is every operation in order; ``mutations`` only the label writes.
    ``fail`` names operations that answer with a GraphQL error. ``after_add``
    runs once the label is on — the moment a second writer would get in.
    """

    def __init__(self, trigger_label: str, *, stamp: str = REPO) -> None:
        self.trigger_label = trigger_label
        self.labels: list[dict[str, str]] = []
        self.state_type = "backlog"
        self.stamp = stamp
        self.ops: list[str] = []
        self.mutations: list[tuple[str, str, str]] = []
        self.comments: list[str] = []
        self.fail: set[str] = set()
        self.after_add: Callable[[], Awaitable[None]] | None = None

    @property
    def labelled(self) -> bool:
        return any(label["id"] == LABEL_ID for label in self.labels)

    def _issue(self) -> dict[str, Any]:
        return {
            "id": NATIVE_ID,
            "identifier": KEY,
            "title": "fix(api): retries storm the upstream",
            "description": "## Evidence\n\n`upstream_timeout` x 412",
            "url": URL,
            "labels": {"nodes": [dict(label) for label in self.labels]},
            "state": {"type": self.state_type},
            "project": {"id": "proj-1", "name": "Backend", "description": f"repo: {self.stamp}", "content": ""},
            "team": {"id": TEAM_ID, "key": "ENG"},
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query, variables = payload["query"], payload["variables"]
        op = next(
            name
            for name in ("issueAddLabel", "issueRemoveLabel", "commentCreate", "issueLabels", "IssueForTrigger")
            if name in query
        )
        self.ops.append(op)
        if op in self.fail:
            return httpx.Response(200, json={"errors": [{"message": f"{op} refused by the fake"}]})
        if op == "IssueForTrigger":
            assert variables["id"] in (KEY, NATIVE_ID), variables
            return httpx.Response(200, json={"data": {"issue": self._issue()}})
        if op == "issueLabels":
            names = [clause["name"]["eqIgnoreCase"] for clause in variables["filter"]["or"]]
            nodes = [
                # A group label and another team's label share the name: neither may be chosen.
                {"id": "lbl-group", "name": self.trigger_label, "isGroup": True, "retiredAt": None, "team": None},
                {
                    "id": "lbl-other-team",
                    "name": self.trigger_label,
                    "isGroup": False,
                    "retiredAt": None,
                    "team": {"id": "team-other"},
                },
                {"id": LABEL_ID, "name": self.trigger_label, "isGroup": False, "retiredAt": None, "team": None},
            ]
            matching = [node for node in nodes if node["name"].lower() in {n.lower() for n in names}]
            return httpx.Response(
                200,
                json={
                    "data": {"issue": {"id": NATIVE_ID, "team": {"id": TEAM_ID}}, "issueLabels": {"nodes": matching}}
                },
            )
        if op == "issueAddLabel":
            assert variables == {"id": NATIVE_ID, "labelId": LABEL_ID}, variables
            self.mutations.append(("add", variables["id"], variables["labelId"]))
            self.labels.append({"id": LABEL_ID, "name": self.trigger_label})
            if self.after_add is not None:
                hook, self.after_add = self.after_add, None
                await hook()
            return httpx.Response(200, json={"data": {"issueAddLabel": {"success": True}}})
        if op == "issueRemoveLabel":
            self.mutations.append(("remove", variables["id"], variables["labelId"]))
            self.labels = [label for label in self.labels if label["id"] != variables["labelId"]]
            return httpx.Response(200, json={"data": {"issueRemoveLabel": {"success": True}}})
        self.comments.append(variables["body"])
        return httpx.Response(200, json={"data": {"commentCreate": {"success": True}}})

    def label_webhook(self) -> dict[str, Any]:
        """The Issue update Linear delivers after the trigger label went on."""
        return {
            "type": "Issue",
            "action": "update",
            "actor": {"name": "The key's owner"},
            "data": {
                "id": NATIVE_ID,
                "identifier": KEY,
                "labelIds": [LABEL_ID],
                "labels": [{"id": LABEL_ID, "name": self.trigger_label}],
            },
            "updatedFrom": {"labelIds": []},
        }


@pytest.fixture
async def wired(api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    app = api_client.app
    # The service's memory of the labels it put on is per process: give each
    # test its own, so one test's acceptance never answers another's webhook.
    monkeypatch.setattr(linear_sync, "_OWN_LABELS", {}, raising=False)
    await app.state.db.execute(
        "TRUNCATE TABLE healing_cases, issues, jobs, watcher_stacks, tracker_dispatch_notices,"
        " tracker_dispatch_failures CASCADE"
    )
    stacks = WatcherStacksRepository(app.state.db)
    await stacks.upsert(WatcherStack(repo=REPO, logql='{compose_project="x"}', tracker="linear"))
    app.state.watcher_stacks_repo = stacks

    trigger_label = (await app.state.integration_repo.get_trigger_labels())[0]
    linear = FakeLinear(trigger_label)
    http = httpx.AsyncClient(transport=httpx.MockTransport(linear.handle))
    app.state.linear_sync = LinearSyncService("lin_api_fake", {}, http_client=http)

    # The real executor creates the job row and reports to the case ledger;
    # only the pipeline run itself is dropped.
    executor = app.state.issue_executor
    executor._track_task = lambda coro, **_: coro.close()
    return {"app": app, "cases": app.state.healing_cases, "stacks": stacks, "linear": linear}


async def _draft(cases: HealingCaseService, *, repo: str = REPO) -> str:
    case = await cases.open_case(repo=repo, origin="watcher", title="a finding", tracker=TrackerRef("linear", KEY, URL))
    assert case["stage"] == "tracked", case["stage"]
    return str(case["id"])


async def _jobs(app: Any) -> list[dict[str, Any]]:
    rows, _total = await app.state.jobs_repo.list_all(limit=50, offset=0)
    return list(rows)


async def _stage(api_client: AsyncClient, case_id: str) -> str:
    return str((await api_client.get(f"/api/v1/cases/{case_id}")).json()["stage"])


async def _assert_nothing_started(api_client: AsyncClient, wired: dict[str, Any], case_id: str) -> None:
    assert await _jobs(wired["app"]) == []
    assert await _stage(api_client, case_id) == "tracked"


@pytest.mark.asyncio
async def test_accepting_a_linear_draft_labels_the_ticket_and_dispatches_once(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    linear: FakeLinear = wired["linear"]
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={"accepted_by": "Alex"})

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "dispatched" and body["tracker"] == "linear" and body["tracker_key"] == KEY
    assert body["accepted_by"] == "Alex" and body["job_id"]

    # The label went on, once, and it is the applicable one: not the group
    # label, not another team's label of the same name.
    assert linear.mutations == [("add", NATIVE_ID, LABEL_ID)]
    assert linear.labelled

    jobs = await _jobs(wired["app"])
    assert [job["job_id"] for job in jobs] == [body["job_id"]]
    assert jobs[0]["repo"] == REPO and jobs[0]["dispatched_by"] == "Alex"
    issue = await wired["app"].state.issues_repo.get_by_tracker_ref("linear", None, KEY)
    assert issue is not None and issue["id"] == body["issue_id"]
    assert issue["created_by"] == "case-accept" and issue["status"] == "triaging" and issue["repo"] == REPO

    # Nobody moved the stage by hand: the job's dispatch accepted the case.
    detail = (await api_client.get(f"/api/v1/cases/{case_id}")).json()
    assert detail["stage"] == "accepted", detail["stage"]
    assert [job["job_id"] for job in detail["jobs"]] == [body["job_id"]]
    # Linear is told like any label dispatch.
    assert len(linear.comments) == 1 and body["job_id"] in linear.comments[0]


async def _finish_the_ticket(linear: FakeLinear) -> None:
    linear.state_type = "completed"


async def _accept(api_client: AsyncClient, case_id: str) -> dict[str, Any]:
    accepted = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})
    assert accepted.status_code == 202, accepted.text
    return dict(accepted.json())


async def _fail_the_job(app: Any, accepted: dict[str, Any]) -> None:
    """What the executor's terminal path does when a job fails."""
    await app.state.jobs_repo.update(accepted["job_id"], status="running")
    await app.state.jobs_repo.update(accepted["job_id"], status="failed")
    await app.state.issues_repo.update(accepted["issue_id"], status="open")


@pytest.mark.asyncio
async def test_the_label_webhook_the_acceptance_causes_starts_no_second_job(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    linear: FakeLinear = wired["linear"]
    case_id = await _draft(wired["cases"])
    accepted = await _accept(api_client, case_id)
    reads = linear.ops.count("IssueForTrigger")

    # Linear now delivers the label change embry0 itself made.
    echo = await api_client.post("/api/v1/webhook/linear", json=linear.label_webhook())

    assert echo.status_code == 200, echo.text
    assert echo.json()["status"] == "ignored" and echo.json().get("skip") == "own_label", echo.json()
    assert [job["job_id"] for job in await _jobs(wired["app"])] == [accepted["job_id"]]
    assert await _stage(api_client, case_id) == "accepted"
    # Known for what it is: no round trip to Linear, and the ticket is not
    # told "did not start a second job" about a label nobody applied by hand.
    assert linear.ops.count("IssueForTrigger") == reads
    assert len(linear.comments) == 1 and "Started work" in linear.comments[0], linear.comments


@pytest.mark.asyncio
async def test_that_webhook_starts_no_second_job_in_a_process_that_did_not_label(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    """A second orchestrator process, or this one after a restart, has no
    memory of the label: the delivery then dedupes on the tracked ticket."""
    linear: FakeLinear = wired["linear"]
    case_id = await _draft(wired["cases"])
    accepted = await _accept(api_client, case_id)
    linear_sync._OWN_LABELS.clear()

    echo = await api_client.post("/api/v1/webhook/linear", json=linear.label_webhook())

    assert echo.status_code == 200, echo.text
    assert echo.json()["status"] == "ignored" and echo.json().get("dedupe") == "live_job", echo.json()
    assert [job["job_id"] for job in await _jobs(wired["app"])] == [accepted["job_id"]]


@pytest.mark.asyncio
async def test_that_webhook_is_not_a_retry_request_when_the_job_has_already_failed(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    """A label applied after a failed job is how a person asks for a retry.
    The acceptance's own label was applied before the job, however late its
    delivery arrives — and it is expected once, not for ever."""
    linear: FakeLinear = wired["linear"]
    app = wired["app"]
    accepted = await _accept(api_client, await _draft(wired["cases"]))
    await _fail_the_job(app, accepted)

    echo = await api_client.post("/api/v1/webhook/linear", json=linear.label_webhook())

    assert echo.json().get("skip") == "own_label", echo.json()
    assert [job["job_id"] for job in await _jobs(app)] == [accepted["job_id"]]

    # A person now takes the label off and puts it back: that one is a retry.
    retry = await api_client.post("/api/v1/webhook/linear", json=linear.label_webhook())
    assert retry.json().get("action") == "dispatched", retry.json()
    assert len(await _jobs(app)) == 2


@pytest.mark.asyncio
async def test_the_expectation_of_that_webhook_expires(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    """A delivery that never came must not swallow a hand-applied label later."""
    linear: FakeLinear = wired["linear"]
    app = wired["app"]
    accepted = await _accept(api_client, await _draft(wired["cases"]))
    await _fail_the_job(app, accepted)
    assert set(linear_sync._OWN_LABELS) == {KEY, NATIVE_ID}
    for ticket_id in linear_sync._OWN_LABELS:
        linear_sync._OWN_LABELS[ticket_id] -= linear_sync._OWN_LABEL_ECHO_SECONDS

    relabel = await api_client.post("/api/v1/webhook/linear", json=linear.label_webhook())

    assert relabel.json().get("action") == "dispatched", relabel.json()
    assert len(await _jobs(app)) == 2


@pytest.mark.asyncio
async def test_a_refused_acceptance_leaves_no_webhook_to_dispatch_the_ticket(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    """The label went on and came off. Its delivery was read while the label
    was still there — it must not dispatch what the acceptance refused."""
    linear: FakeLinear = wired["linear"]
    linear.after_add = lambda: _finish_the_ticket(linear)
    case_id = await _draft(wired["cases"])
    refused = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})
    assert refused.status_code == 409 and "taken off again" in refused.json()["detail"], refused.text
    # The state Linear's delivery describes: labelled, and dispatchable again.
    linear.state_type = "backlog"
    linear.labels.append({"id": LABEL_ID, "name": linear.trigger_label})

    echo = await api_client.post("/api/v1/webhook/linear", json=linear.label_webhook())

    assert echo.json().get("skip") == "own_label", echo.json()
    await _assert_nothing_started(api_client, wired, case_id)


@pytest.mark.asyncio
async def test_a_label_linear_does_not_add_dispatches_nothing(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    linear: FakeLinear = wired["linear"]
    linear.fail.add("issueAddLabel")
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})

    assert r.status_code == 502, r.text
    assert "Linear did not confirm" in r.json()["detail"] and r.json()["detail"].endswith("No job was started.")
    assert "issueAddLabel" in linear.ops and not linear.labelled
    assert await wired["app"].state.issues_repo.get_by_tracker_ref("linear", None, KEY) is None
    await _assert_nothing_started(api_client, wired, case_id)


@pytest.mark.asyncio
async def test_a_ticket_that_cannot_be_dispatched_is_never_labelled(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    """Known before the ticket is touched, so the ticket is not touched: a
    label that goes on sends a webhook that taking it off does not recall."""
    linear: FakeLinear = wired["linear"]
    linear.state_type = "completed"
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})

    assert r.status_code == 409, r.text
    assert "'completed' is not dispatchable" in r.json()["detail"]
    assert linear.mutations == []
    await _assert_nothing_started(api_client, wired, case_id)


@pytest.mark.asyncio
async def test_a_dispatch_refused_after_the_label_takes_the_label_off_again(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    linear: FakeLinear = wired["linear"]
    # Someone completes the ticket between the look and the label.
    linear.after_add = lambda: _finish_the_ticket(linear)
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "'completed' is not dispatchable" in detail
    assert f"label was added to {KEY} and taken off again; no job was started" in detail
    assert linear.mutations == [("add", NATIVE_ID, LABEL_ID), ("remove", NATIVE_ID, LABEL_ID)]
    assert not linear.labelled
    await _assert_nothing_started(api_client, wired, case_id)


@pytest.mark.asyncio
async def test_a_rate_limit_reached_after_the_label_takes_the_label_off_again(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    linear: FakeLinear = wired["linear"]
    executor = wired["app"].state.issue_executor
    answers = [None, DispatchDecision(allowed=False, limit="rate_limit", message="5 dispatches in the last hour")]

    async def _gate(*, actor: str | None = None) -> DispatchDecision | None:
        return answers.pop(0)

    executor.dispatch_refusal = _gate
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})

    assert r.status_code == 429, r.text
    assert "dispatch rate limit reached" in r.json()["detail"] and "taken off again" in r.json()["detail"]
    assert answers == [], "the gate must be asked before the label and again by the dispatch"
    assert linear.mutations == [("add", NATIVE_ID, LABEL_ID), ("remove", NATIVE_ID, LABEL_ID)]
    await _assert_nothing_started(api_client, wired, case_id)


@pytest.mark.asyncio
async def test_a_label_that_cannot_be_taken_off_again_is_said_in_the_error(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    linear: FakeLinear = wired["linear"]
    linear.after_add = lambda: _finish_the_ticket(linear)
    linear.fail.add("issueRemoveLabel")
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "could NOT be taken off again" in detail and "left labelled with no job" in detail
    assert linear.labelled and linear.ops.count("issueRemoveLabel") == 1
    await _assert_nothing_started(api_client, wired, case_id)


@pytest.mark.asyncio
async def test_a_dispatch_that_raises_after_the_label_is_an_error_not_a_success(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    linear: FakeLinear = wired["linear"]
    executor = wired["app"].state.issue_executor

    async def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("the executor is down")

    executor.execute = _boom
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})

    assert r.status_code == 502, r.text
    assert "the dispatch failed" in r.json()["detail"] and "taken off again" in r.json()["detail"]
    assert "job_id" not in r.json()
    assert not linear.labelled
    # The issue row exists and is dispatchable again; no job, case untouched.
    issue = await wired["app"].state.issues_repo.get_by_tracker_ref("linear", None, KEY)
    assert issue is not None and issue["status"] == "open"
    await _assert_nothing_started(api_client, wired, case_id)


@pytest.mark.asyncio
async def test_a_ticket_that_resolves_to_another_repo_is_refused_untouched(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    """The dispatch takes the repo from the Linear project; the case carries
    the stack's. A job run elsewhere would never be linked to this case."""
    linear: FakeLinear = wired["linear"]
    linear.stamp = "acme/frontend"
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})

    assert r.status_code == 409, r.text
    assert "resolves to `acme/frontend`" in r.json()["detail"] and f"not `{REPO}`" in r.json()["detail"]
    # Not "added and removed": the label webhook would have dispatched it.
    assert linear.mutations == [] and not linear.labelled
    assert await wired["app"].state.issues_repo.get_by_tracker_ref("linear", None, KEY) is None
    await _assert_nothing_started(api_client, wired, case_id)


@pytest.mark.asyncio
async def test_a_ticket_already_labelled_is_dispatched_without_a_second_label(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    """Labelled in Linear, the webhook lost: Accept still dispatches, and a
    refusal would not take off a label this call did not put on."""
    linear: FakeLinear = wired["linear"]
    linear.labels.append({"id": LABEL_ID, "name": linear.trigger_label})
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})

    assert r.status_code == 202, r.text
    assert linear.mutations == [] and "issueLabels" not in linear.ops
    assert len(await _jobs(wired["app"])) == 1
    assert await _stage(api_client, case_id) == "accepted"


@pytest.mark.asyncio
async def test_a_second_dispatcher_that_wins_keeps_its_label_and_its_job(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    """Another orchestrator process handles the label webhook before this one
    dispatches: one job, the label stays, and this call claims no job."""
    linear: FakeLinear = wired["linear"]
    app = wired["app"]

    async def _other_process_dispatches() -> None:
        issue_id = await app.state.issues_repo.create(
            title=f"[{KEY}] t", body="", labels=[], repo=REPO, created_by="linear-webhook",
            tracker="linear", tracker_key=KEY, tracker_url=URL, tracker_native_id=NATIVE_ID,
        )  # fmt: skip
        await app.state.issues_repo.update(issue_id, status="triaging")
        await app.state.issue_executor.execute(issue_id)

    linear.after_add = _other_process_dispatches
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})

    assert r.status_code == 409, r.text
    assert "this acceptance started no job of its own" in r.json()["detail"]
    assert linear.mutations == [("add", NATIVE_ID, LABEL_ID)] and linear.labelled
    assert len(await _jobs(app)) == 1


@pytest.mark.asyncio
async def test_a_ticket_linear_cannot_serve_is_502_and_untouched(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    linear: FakeLinear = wired["linear"]
    linear.fail.add("IssueForTrigger")
    case_id = await _draft(wired["cases"])

    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})

    assert r.status_code == 502 and r.json()["detail"] == f"Linear could not serve {KEY}", r.text
    assert linear.mutations == []
    await _assert_nothing_started(api_client, wired, case_id)


@pytest.mark.asyncio
async def test_without_linear_configured_the_draft_is_503(api_client: AsyncClient, wired: dict[str, Any]) -> None:
    wired["app"].state.linear_sync = None
    case_id = await _draft(wired["cases"])
    r = await api_client.post(f"/api/v1/cases/{case_id}/accept", json={})
    assert r.status_code == 503 and "Linear integration is not configured" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_merge_scope_key_accepts_a_linear_draft_of_an_enabled_stack_only(
    api_client: AsyncClient, wired: dict[str, Any]
) -> None:
    """The route is on the merge-scope allowlist. That key reaches Linear
    drafts now — and still nothing outside this orchestrator's own stacks."""
    from embry0.api.deps import bust_api_key_cache

    app = wired["app"]
    await app.state.db.execute("TRUNCATE TABLE api_keys CASCADE")
    _row, token = await app.state.api_keys_repo.create(name="chat", scope="merge", user_id=None, created_by="tester")
    app.state.config.auth_dev_mode = False
    app.state.config.api_key = "master-key"
    bust_api_key_cache()
    auth = {"Authorization": f"Bearer {token}"}
    try:
        # Proof the key is the narrow one: an ordinary mutation is refused.
        blocked = await api_client.post("/api/v1/watcher/run", headers=auth)
        assert blocked.status_code == 403 and "merge-scope" in blocked.json()["detail"], blocked.text

        elsewhere = await _draft(wired["cases"], repo="acme/other")
        refused = await api_client.post(f"/api/v1/cases/{elsewhere}/accept", json={}, headers=auth)
        assert refused.status_code == 403, refused.text
        assert "not a stack this embry0 watches" in refused.json()["detail"]
        assert wired["linear"].ops == [] and await _jobs(app) == []

        await app.state.db.execute("DELETE FROM healing_cases")
        own = await _draft(wired["cases"])
        accepted = await api_client.post(f"/api/v1/cases/{own}/accept", json={}, headers=auth)
        assert accepted.status_code == 202, accepted.text
        assert len(await _jobs(app)) == 1
    finally:
        app.state.config.auth_dev_mode = True
        app.state.config.api_key = ""
        bust_api_key_cache()
