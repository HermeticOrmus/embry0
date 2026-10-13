"""GET /jobs/{id}/logs/events pages past the 500-row cap (#203)."""

import pytest

pytestmark = pytest.mark.asyncio


async def _seed(api_client, n: int) -> str:
    jobs = api_client.app.state.jobs_repo
    job_id = await jobs.create(repo="o/r", task="t")
    for i in range(n):
        await jobs.append_log_event(job_id, {"type": "progress", "n": i})
    return job_id


async def test_paging_reaches_the_tail_of_a_long_log(api_client):
    total = 1203
    job_id = await _seed(api_client, total)

    seen: list[int] = []
    since = 0
    for _ in range(10):
        resp = await api_client.get(f"/api/v1/jobs/{job_id}/logs/events", params={"since_seq": since, "limit": 500})
        assert resp.status_code == 200
        body = resp.json()
        assert "next_since_seq" in body, f"response has no paging cursor: keys={sorted(body)}"
        seen.extend(e["n"] for e in body["events"])
        if not body["has_more"]:
            break
        assert body["next_since_seq"] > since, "cursor did not advance"
        since = body["next_since_seq"]

    assert seen == list(range(total)), f"paging returned {len(seen)} of {total} events; last={seen[-1:]}"


async def test_each_event_carries_its_seq_and_cursor_is_the_last_one(api_client):
    job_id = await _seed(api_client, 3)
    body = (await api_client.get(f"/api/v1/jobs/{job_id}/logs/events", params={"limit": 2})).json()
    seqs = [e["event_seq"] for e in body["events"]]
    assert len(seqs) == 2 and seqs[0] < seqs[1]
    assert body["next_since_seq"] == seqs[-1]
    assert body["has_more"] is True

    rest = (await api_client.get(f"/api/v1/jobs/{job_id}/logs/events", params={"since_seq": seqs[-1]})).json()
    assert [e["n"] for e in rest["events"]] == [2]
    assert rest["has_more"] is False

    # An empty page keeps the caller's cursor.
    empty = (
        await api_client.get(f"/api/v1/jobs/{job_id}/logs/events", params={"since_seq": rest["next_since_seq"]})
    ).json()
    assert empty == {"events": [], "next_since_seq": rest["next_since_seq"], "has_more": False}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1001}, {"since_seq": -1}])
async def test_out_of_range_paging_params_are_refused(api_client, params):
    resp = await api_client.get("/api/v1/jobs/job-x/logs/events", params=params)
    assert resp.status_code == 422, f"{params} accepted: {resp.status_code}"
