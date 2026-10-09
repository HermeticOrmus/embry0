"""LogQL startup validation (#270) — Loki is the parser of record.

The escaped-quote cases reproduce the 2026-09-09 → 09-18 outage: a
``WATCHER_LOGQL`` written with backslash-escaped quotes in ``.env`` reached
Loki verbatim, every tick got ``400 Bad Request``, and nothing said so for
217 ticks.
"""

import httpx
import pytest

from embry0.services.logql_check import LogQLStatus, check_logql

LOKI = "http://loki.test:3100"
GOOD = '{compose_project="acme"} |~ "(?i)(error|exception|traceback)"'
# What docker compose hands the process when .env says
# WATCHER_LOGQL={compose_project=\"acme\"} — the backslashes survive.
ESCAPED = '{compose_project=\\"acme\\"} |~ \\"(?i)(error|exception|traceback)\\"'
# Verbatim body Loki 3.x returns for the query above.
LOKI_PARSE_ERROR = "parse error at line 1, col 18: syntax error: unexpected IDENTIFIER, expecting STRING"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_query_loki_accepts_is_ok():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        seen["path"] = request.url.path
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    async with _client(handler) as client:
        result = await check_logql(LOKI, GOOD, http_client=client)

    assert result.status is LogQLStatus.OK
    assert result.ok
    # A log query is rejected by the instant endpoint, so the probe has to be
    # a range query; limit=1 keeps it free.
    assert seen["path"] == "/loki/api/v1/query_range"
    assert seen["query"] == GOOD
    assert seen["limit"] == "1"


@pytest.mark.asyncio
async def test_backslash_escaped_quotes_are_a_parse_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["query"] == ESCAPED  # sent verbatim, not "repaired"
        return httpx.Response(400, text=LOKI_PARSE_ERROR)

    async with _client(handler) as client:
        result = await check_logql(LOKI, ESCAPED, http_client=client)

    assert result.status is LogQLStatus.INVALID
    assert not result.ok
    assert "parse error" in result.detail
    assert "expecting STRING" in result.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 422])
async def test_only_400_and_422_judge_the_query(status_code: int):
    async with _client(lambda r: httpx.Response(status_code, text="bad things")) as client:
        result = await check_logql(LOKI, GOOD, http_client=client)
    assert result.status is LogQLStatus.INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 404, 405])
async def test_other_4xx_blame_the_endpoint_not_the_query(status_code: int):
    """A wrong path, or WATCHER_LOKI_URL pointing at Prometheus, answers 404.
    Telling the operator to fix a query that is fine sends them the wrong way."""
    async with _client(lambda r: httpx.Response(status_code, text="404 page not found")) as client:
        result = await check_logql(LOKI, GOOD, http_client=client)
    assert result.status is LogQLStatus.MISCONFIGURED
    assert not result.ok
    assert str(status_code) in result.detail


@pytest.mark.asyncio
async def test_connection_error_is_unreachable_not_invalid():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client(handler) as client:
        result = await check_logql(LOKI, ESCAPED, http_client=client)

    assert result.status is LogQLStatus.UNREACHABLE
    assert "connection refused" in result.detail


@pytest.mark.asyncio
async def test_timeout_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async with _client(handler) as client:
        result = await check_logql(LOKI, GOOD, http_client=client)
    assert result.status is LogQLStatus.UNREACHABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
async def test_loki_side_trouble_is_unreachable(status_code: int):
    """A sick or throttling Loki says nothing about the query."""
    async with _client(lambda r: httpx.Response(status_code, text="nope")) as client:
        result = await check_logql(LOKI, GOOD, http_client=client)
    assert result.status is LogQLStatus.UNREACHABLE
    assert str(status_code) in result.detail


@pytest.mark.asyncio
async def test_detail_is_bounded():
    async with _client(lambda r: httpx.Response(400, text="x" * 5000)) as client:
        result = await check_logql(LOKI, GOOD, http_client=client)
    assert len(result.detail) <= 500
