"""POST /api/v1/cases/{id}/close (#410) — settling a case a human has decided.

The mirror of ``accept``. Runs against the real ledger: the case and its rows
are real, so a refusal really is "nothing was written".
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from embry0.services.healing.service import HealingCaseService, TrackerRef

REPO = "acme/backend"
URL = f"https://github.com/{REPO}/issues/397"


@pytest.fixture
async def cases(api_client: AsyncClient) -> HealingCaseService:
    await api_client.app.state.db.execute("TRUNCATE TABLE healing_cases, issues CASCADE")
    return api_client.app.state.healing_cases


async def _case(cases: HealingCaseService) -> str:
    case = await cases.open_case(
        repo=REPO, origin="watcher", title="a finding", tracker=TrackerRef("github", "397", URL)
    )
    return str(case["id"])


@pytest.mark.asyncio
async def test_close_settles_the_case_with_the_path_it_is_given(api_client: AsyncClient, cases) -> None:
    case_id = await _case(cases)
    r = await api_client.post(
        f"/api/v1/cases/{case_id}/close",
        json={"resolution_path": "out_of_band", "note": "fixed by PR #856", "closed_by": "Alex"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["stage"], body["resolution_path"], body["closed_by"]) == ("closed", "out_of_band", "Alex")

    stored = await cases.get(case_id)
    assert (stored["stage"], stored["resolution_path"], stored["note"]) == ("closed", "out_of_band", "fixed by PR #856")
    assert stored["closed_at"] is not None


@pytest.mark.asyncio
async def test_close_accepts_abandoned_as_a_path(api_client: AsyncClient, cases) -> None:
    # Migration 84 added it; without that the write would fail the CHECK.
    case_id = await _case(cases)
    r = await api_client.post(
        f"/api/v1/cases/{case_id}/close", json={"resolution_path": "abandoned", "closed_by": "Alex"}
    )
    assert r.status_code == 200, r.text
    assert (await cases.get(case_id))["resolution_path"] == "abandoned"


@pytest.mark.asyncio
async def test_a_case_with_no_path_of_its_own_needs_one(api_client: AsyncClient, cases) -> None:
    case_id = await _case(cases)
    r = await api_client.post(f"/api/v1/cases/{case_id}/close", json={"closed_by": "Alex"})
    assert r.status_code == 422, r.text
    assert "resolution_path" in r.json()["detail"]
    assert (await cases.get(case_id))["stage"] == "tracked", "a refused close must write nothing"


@pytest.mark.asyncio
async def test_an_unknown_path_is_refused_before_the_database_sees_it(api_client: AsyncClient, cases) -> None:
    case_id = await _case(cases)
    r = await api_client.post(
        f"/api/v1/cases/{case_id}/close", json={"resolution_path": "because_i_said_so", "closed_by": "Alex"}
    )
    assert r.status_code == 422, r.text
    assert "because_i_said_so" in r.json()["detail"] and "abandoned" in r.json()["detail"]
    assert (await cases.get(case_id))["stage"] == "tracked"


@pytest.mark.asyncio
async def test_closing_a_closed_case_is_refused(api_client: AsyncClient, cases) -> None:
    case_id = await _case(cases)
    first = await api_client.post(
        f"/api/v1/cases/{case_id}/close", json={"resolution_path": "cancelled", "closed_by": "Alex"}
    )
    assert first.status_code == 200, first.text
    again = await api_client.post(
        f"/api/v1/cases/{case_id}/close", json={"resolution_path": "abandoned", "closed_by": "Alex"}
    )
    assert again.status_code == 409, again.text
    # The first resolution stands: a second close cannot rewrite how it ended.
    assert (await cases.get(case_id))["resolution_path"] == "cancelled"


@pytest.mark.asyncio
async def test_closing_a_case_that_does_not_exist_is_a_404(api_client: AsyncClient, cases) -> None:
    r = await api_client.post("/api/v1/cases/hc-nope/close", json={"resolution_path": "cancelled", "closed_by": "T"})
    assert r.status_code == 404, r.text
    # The route's own 404, not FastAPI's for a route that is not there — which
    # is what this asserted before the route existed, and so proved nothing.
    assert r.json()["detail"] == "case not found"
