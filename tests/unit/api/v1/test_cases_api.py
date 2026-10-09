"""GET /api/v1/cases and /api/v1/cases/{id} — the ledger's read API (#278)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.config import Embry0Config
from embry0.services.healing.service import HealingCaseService, TrackerRef


@pytest.fixture
async def cases(api_client: AsyncClient) -> HealingCaseService:
    await api_client.app.state.db.execute("TRUNCATE TABLE healing_cases CASCADE")
    return api_client.app.state.healing_cases


async def _seed_many(service: HealingCaseService, n: int, repo: str = "acme/widgets") -> list[str]:
    """n cases sharing ONE timestamp — the worst case for stable paging."""
    at = datetime(2026, 9, 1, tzinfo=UTC)
    return [(await service.open_case(repo=repo, origin="watcher", title=f"case {i}", at=at))["id"] for i in range(n)]


@pytest.mark.asyncio
async def test_empty_list(api_client: AsyncClient, cases: HealingCaseService) -> None:
    r = await api_client.get("/api/v1/cases")
    assert r.status_code == 200, r.text
    assert r.json() == {"cases": [], "total": 0, "limit": 50, "next_cursor": None}


async def _walk(api_client: AsyncClient, **params: object) -> list[str]:
    seen: list[str] = []
    cursor = None
    for _ in range(20):
        query = {**params, **({"cursor": cursor} if cursor else {})}
        body = (await api_client.get("/api/v1/cases", params=query)).json()
        seen += [c["id"] for c in body["cases"]]
        cursor = body["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("pagination never ended")


@pytest.mark.asyncio
async def test_pages_cover_every_case_exactly_once(api_client: AsyncClient, cases: HealingCaseService) -> None:
    ids = await _seed_many(cases, 7)
    first = (await api_client.get("/api/v1/cases", params={"limit": 3})).json()
    assert (first["total"], first["limit"], len(first["cases"])) == (7, 3, 3)
    assert first["next_cursor"]

    seen = await _walk(api_client, limit=3)
    assert len(seen) == 7
    assert set(seen) == set(ids)
    # A page that ends exactly on the last row still terminates.
    assert len(await _walk(api_client, limit=7)) == 7


@pytest.mark.asyncio
async def test_a_case_opened_mid_walk_neither_repeats_nor_drops_a_row(
    api_client: AsyncClient, cases: HealingCaseService
) -> None:
    ids = await _seed_many(cases, 4)
    first = (await api_client.get("/api/v1/cases", params={"limit": 2})).json()
    await cases.open_case(repo="acme/widgets", origin="human", title="arrived mid-walk")
    rest = (await api_client.get("/api/v1/cases", params={"limit": 2, "cursor": first["next_cursor"]})).json()
    walked = [c["id"] for c in first["cases"] + rest["cases"]]
    assert sorted(walked) == sorted(ids)


@pytest.mark.asyncio
async def test_a_cursor_this_endpoint_did_not_issue_is_400(api_client: AsyncClient, cases: HealingCaseService) -> None:
    from embry0.api.v1.cases import encode_cursor

    naive = encode_cursor(datetime(2026, 9, 1), "hc-1")  # noqa: DTZ001 — the point of the test
    for cursor in ("not-a-cursor", naive):
        r = await api_client.get("/api/v1/cases", params={"cursor": cursor})
        assert r.status_code == 400, cursor


@pytest.mark.asyncio
async def test_a_row_with_an_unknown_value_is_served_not_a_500(
    api_client: AsyncClient, cases: HealingCaseService
) -> None:
    """origin has no CHECK (open vocabulary), so manual SQL or a rolled-back
    newer writer can leave a value this build does not know. One such row
    must not take the whole page down."""
    await cases.open_case(repo="acme/widgets", origin="watcher", title="known")
    await api_client.app.state.db.execute(
        """
        INSERT INTO healing_cases (id, repo, origin, title, severity, fixability)
        VALUES ('hc-from-the-future', 'acme/widgets', 'sentry', 'unknown origin', 'sev-9', 'maybe')
        """
    )
    listed = await api_client.get("/api/v1/cases")
    assert listed.status_code == 200, listed.text
    assert {c["origin"] for c in listed.json()["cases"]} == {"watcher", "sentry"}
    one = await api_client.get("/api/v1/cases/hc-from-the-future")
    assert one.status_code == 200, one.text
    assert (one.json()["origin"], one.json()["severity"]) == ("sentry", "sev-9")
    # The FILTER stays strict: asking for a source this build does not know is the caller's error.
    assert (await api_client.get("/api/v1/cases", params={"origin": "sentry"})).status_code == 422


@pytest.mark.asyncio
async def test_outcome_filters(api_client: AsyncClient, cases: HealingCaseService) -> None:
    """#285 asks "which findings did a deployed fix heal?" — answerable without
    going through stage=verified, which also holds cases nothing fixed."""
    at = datetime(2026, 9, 2, tzinfo=UTC)

    async def new(title: str) -> str:
        return (await cases.open_case(repo="acme/widgets", origin="watcher", title=title))["id"]

    fixed = await new("fixed and healed")
    await cases.record_merge(fixed, merge_sha="def", resolution_path="embry0", at=at)
    await cases.record_deploy(fixed, sha="def", marker_at=at)
    await cases.record_verdict(fixed, "healed", evidence={})
    by_itself = await new("healed by itself")
    await cases.record_verdict(by_itself, "healed", evidence={})
    human = await new("merged by a human, not deployed")
    await cases.record_merge(human, merge_sha="abc", resolution_path="out_of_band", at=at)
    await new("untouched")

    async def ids(**params: object) -> tuple[set[str], int]:
        body = (await api_client.get("/api/v1/cases", params=params)).json()
        return {c["id"] for c in body["cases"]}, body["total"]

    assert await ids(stage="verified") == ({fixed, by_itself}, 2)  # the misleading handle
    assert await ids(fix_deployed="true") == ({fixed}, 1)
    assert (await ids(fix_deployed="false"))[1] == 3
    assert await ids(healed_verdict="healed", fix_deployed="true") == ({fixed}, 1)
    assert await ids(resolution_path="self_resolved") == ({by_itself}, 1)
    assert await ids(resolution_path="out_of_band") == ({human}, 1)
    assert (await ids(healed_verdict="pending"))[1] == 2
    # The SQL filter and the response field are the same definition.
    every = (await api_client.get("/api/v1/cases")).json()["cases"]
    assert {c["id"] for c in every if c["fix_deployed"]} == {fixed}
    for bad in ({"resolution_path": "magic"}, {"healed_verdict": "cured"}, {"fix_deployed": "perhaps"}):
        assert (await api_client.get("/api/v1/cases", params=bad)).status_code == 422


@pytest.mark.asyncio
async def test_newest_first(api_client: AsyncClient, cases: HealingCaseService) -> None:
    old = await cases.open_case(repo="acme/widgets", origin="human", title="old")
    new = await cases.open_case(repo="acme/widgets", origin="human", title="new")
    await api_client.app.state.db.execute(
        "UPDATE healing_cases SET created_at = created_at - $1::interval WHERE id = $2", timedelta(days=1), old["id"]
    )
    body = (await api_client.get("/api/v1/cases")).json()
    assert [c["id"] for c in body["cases"]] == [new["id"], old["id"]]


@pytest.mark.asyncio
async def test_filters_and_total_agree(api_client: AsyncClient, cases: HealingCaseService) -> None:
    await _seed_many(cases, 3, repo="acme/widgets")
    other = await cases.open_case(repo="acme/backend", origin="human", title="x", tracker=TrackerRef("linear", "ENG-1"))
    closed = await cases.open_case(repo="acme/widgets", origin="webhook", title="y")
    await cases.close(closed["id"], resolution_path="cancelled")

    async def ids(**params: object) -> tuple[set[str], int]:
        body = (await api_client.get("/api/v1/cases", params=params)).json()
        return {c["id"] for c in body["cases"]}, body["total"]

    assert await ids(repo="acme/backend") == ({other["id"]}, 1)
    assert await ids(stage="tracked") == ({other["id"]}, 1)
    assert await ids(stage="closed") == ({closed["id"]}, 1)
    assert await ids(origin="webhook") == ({closed["id"]}, 1)
    assert (await ids())[1] == 5
    open_ids, open_total = await ids(open_only="true")
    assert open_total == 4
    assert closed["id"] not in open_ids
    assert await ids(repo="acme/widgets", origin="watcher", open_only="true", limit=2) == (
        (await ids(repo="acme/widgets", origin="watcher", limit=2))[0],
        3,
    )


@pytest.mark.asyncio
async def test_awaiting_decision_is_the_watcher_drafts_nobody_answered(
    api_client: AsyncClient, cases: HealingCaseService
) -> None:
    """The dashboard's Proposals list. `stage=tracked` is not it: that also
    returns a human's ticket and a draft whose ticket is gone from its tracker,
    and `open_only` returns everything short of closed."""
    from embry0.storage.repositories.watcher_runs import WatcherRunsRepository

    db = api_client.app.state.db
    await db.execute("TRUNCATE TABLE watcher_runs CASCADE")
    runs = WatcherRunsRepository(db)
    now = datetime.now(UTC)

    async def draft(title: str, tracker: TrackerRef, repo: str = "acme/widgets") -> str:
        run_id = await runs.create(
            now, now, "proposed", tracker=tracker.kind, tracker_key=tracker.key, repo=repo, detail=title
        )
        opened = await cases.open_case(repo=repo, origin="watcher", title=title, watcher_run_id=run_id, tracker=tracker)
        return str(opened["id"])

    waiting = await draft("waiting, linear", TrackerRef("linear", "ENG-1"))
    waiting_github = await draft("waiting, github", TrackerRef("github", "12"), repo="acme/backend")
    aged = await draft("waiting, older than the cap window", TrackerRef("linear", "ENG-2"))
    await db.execute("UPDATE healing_cases SET detected_at = $1 WHERE id = $2", now - timedelta(days=30), aged)

    # Everything below is `open_only`, and none of it is waiting for an answer.
    gone = await draft("ticket finished in its tracker", TrackerRef("linear", "ENG-3"))
    assert await runs.release_ticket("linear", "ENG-3") == 1
    rejected = await draft("rejected", TrackerRef("linear", "ENG-4"))
    await cases.close(rejected, resolution_path="cancelled")
    merged = await draft("merged out of band", TrackerRef("linear", "ENG-5"))
    await cases.record_merge(merged, merge_sha="abc", resolution_path="out_of_band", at=now)
    await cases.open_case(
        repo="acme/widgets", origin="human", title="a person's ticket", tracker=TrackerRef("linear", "ENG-6")
    )
    await cases.open_case(repo="acme/widgets", origin="watcher", title="deferred: no ticket yet")
    await cases.open_case(repo="acme/widgets", origin="alert", title="an alert")

    async def ids(**params: object) -> tuple[set[str], int]:
        body = (await api_client.get("/api/v1/cases", params=params)).json()
        return {c["id"] for c in body["cases"]}, body["total"]

    assert await ids(awaiting_decision="true") == ({waiting, waiting_github, aged}, 3)
    assert await ids(awaiting_decision="true", repo="acme/backend") == ({waiting_github}, 1)
    # The two filters it must not be mistaken for really do return more.
    assert gone in (await ids(stage="tracked"))[0]
    assert (await ids(open_only="true"))[1] == 8
    # Inside the cap window it is the cap's own rows: one rule, not two.
    repo = api_client.app.state.healing_cases_repo
    assert await repo.count_open_unaccepted_watcher_cases(days=7) == 2
    assert await repo.open_unaccepted_watcher_tracker_keys(days=None) == ["ENG-2", "ENG-1", "12"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [{"stage": "bogus"}, {"origin": "robot"}, {"limit": 0}, {"limit": 201}],
)
async def test_bad_query_values_are_422(api_client: AsyncClient, cases: HealingCaseService, params: dict) -> None:
    assert (await api_client.get("/api/v1/cases", params=params)).status_code == 422


@pytest.mark.asyncio
async def test_detail_carries_jobs_prs_and_history(api_client: AsyncClient, cases: HealingCaseService) -> None:
    state = api_client.app.state
    issue_id = await state.issues_repo.create(title="[ENG-9] fix", repo="acme/widgets")
    job_id = await state.jobs_repo.create(repo="acme/widgets", task="fix", issue_id=issue_id)
    case = await cases.open_case(
        repo="acme/widgets",
        origin="watcher",
        title="boom",
        severity="high",
        tracker=TrackerRef("linear", "ENG-9", "https://l/ENG-9"),
    )
    cid = case["id"]
    old_pr, pr = "https://github.com/acme/widgets/pull/6", "https://github.com/acme/widgets/pull/7"
    await cases.attach_job(cid, job_id)
    await cases.attach_pr(cid, url=old_pr, number=6)
    await cases.close_pr(cid, url=old_pr)
    await cases.attach_pr(cid, url=pr, number=7, head_sha="abc", job_id=job_id, at=datetime(2026, 9, 1, tzinfo=UTC))
    await cases.record_merge(cid, merge_sha="def", resolution_path="embry0", pr_url=pr)
    await cases.record_verdict(cid, "healed", evidence={"before": 9, "after": 0})

    r = await api_client.get(f"/api/v1/cases/{cid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stage"] == "verified"
    assert body["severity"] == "high"
    assert body["tracker"] == {"kind": "linear", "key": "ENG-9", "url": "https://l/ENG-9"}
    assert body["issue_id"] == issue_id
    # The PR that stands for the case is the merged one; both are listed.
    assert body["pr"] == {"url": pr, "number": 7, "head_sha": "abc", "state": "merged"}
    assert [(p["number"], p["state"], p["merge_sha"]) for p in body["prs"]] == [
        (6, "closed", None),
        (7, "merged", "def"),
    ]
    assert body["merge_sha"] == "def"
    assert body["deploy_marker"] is None
    assert body["healed_verdict"]["verdict"] == "healed"
    assert body["healed_verdict"]["evidence"] == {"before": 9, "after": 0}
    assert body["resolution_path"] == "embry0"
    assert (body["occurrence_count"], body["caused_regression_case_id"]) == (1, None)
    assert [j["job_id"] for j in body["jobs"]] == [job_id]
    assert [e["event"] for e in body["events"]] == [
        "opened",
        "accepted",
        "pr_opened",
        "pr_closed",
        "pr_opened",
        "merged",
        "verdict_recorded",
    ]
    assert body["timestamps"]["merged_at"] is not None
    assert body["timestamps"]["closed_at"] is None

    listed = (await api_client.get("/api/v1/cases")).json()["cases"][0]
    assert (listed["jobs_count"], listed["prs_count"]) == (1, 2)
    assert (listed["pr"]["number"], listed["pr"]["state"]) == (7, "merged")
    assert "events" not in listed


@pytest.mark.asyncio
async def test_verified_is_not_fix_deployed(api_client: AsyncClient, cases: HealingCaseService) -> None:
    """OQ2: the API answers "is a fix running?" so no caller infers it from the stage."""
    marker = datetime(2026, 9, 2, tzinfo=UTC)
    healed_alone = (await cases.open_case(repo="acme/widgets", origin="watcher", title="stopped by itself"))["id"]
    await cases.record_verdict(healed_alone, "healed", evidence={})
    fixed = (await cases.open_case(repo="acme/widgets", origin="watcher", title="fixed"))["id"]
    await cases.record_merge(fixed, merge_sha="def", resolution_path="embry0", at=marker)
    merged_only = (await api_client.get(f"/api/v1/cases/{fixed}")).json()
    await cases.record_deploy(fixed, sha="def", marker_at=marker, services=["backend"])
    await cases.record_verdict(fixed, "healed", evidence={})

    alone = (await api_client.get(f"/api/v1/cases/{healed_alone}")).json()
    shipped = (await api_client.get(f"/api/v1/cases/{fixed}")).json()
    assert (alone["stage"], alone["fix_deployed"]) == ("verified", False)
    assert merged_only["fix_deployed"] is False
    assert (shipped["stage"], shipped["fix_deployed"]) == ("verified", True)
    assert shipped["deploy_marker"] == {"sha": "def", "at": shipped["deploy_marker"]["at"], "services": ["backend"]}


@pytest.mark.asyncio
async def test_unknown_case_is_404(api_client: AsyncClient, cases: HealingCaseService) -> None:
    assert (await api_client.get("/api/v1/cases/hc-missing")).status_code == 404


@pytest.mark.asyncio
async def test_cases_require_auth() -> None:
    config = Embry0Config(_env_file=None, auth_dev_mode=False, api_key="real-key")
    app = create_app(config)
    app.state.healing_cases_repo = MagicMock(list_page=AsyncMock(return_value=([], 0)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/cases")).status_code == 401
        assert (await client.get("/api/v1/cases/hc-1")).status_code == 401
        ok = await client.get("/api/v1/cases", headers={"Authorization": "Bearer real-key"})
    assert ok.status_code == 200
