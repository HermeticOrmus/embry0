"""GET /api/v1/logs/query (RAV-1057) — LogQL building, Loki proxying, errors."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
from httpx import ASGITransport, AsyncClient

from embry0.api.app import create_app
from embry0.api.v1.logs import _build_query
from embry0.config import Embry0Config


def _app(loki_url="http://loki.test:3100"):
    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    config = Embry0Config(database_url="postgresql://unused", auth_dev_mode=True, watcher_loki_url=loki_url)
    app = create_app(config, lifespan_override=_noop_lifespan)
    app.state.config = config
    return app


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _loki_response(streams):
    return {"data": {"result": streams}}


def _patch_loki(handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    return patch("embry0.api.v1.logs.httpx.AsyncClient", _factory)


def test_build_query_shapes():
    assert _build_query(None, None) == '{container_name=~".+"}'
    assert _build_query("acme-backend", None) == '{container_name=~".*acme-backend.*"}'
    assert _build_query(None, "(?i)error") == '{container_name=~".+"} |~ "(?i)error"'
    # quotes/backslashes in user input cannot break out of the LogQL string
    assert '\\"' in _build_query(None, 'boom"}{evil')


async def test_query_returns_parsed_lines_newest_first():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=_loki_response(
                [
                    {
                        "stream": {"container_name": "acme-backend"},
                        "values": [["2000000000000000000", "older line"], ["3000000000000000000", "newest line"]],
                    },
                    {"stream": {"container_name": "acme-ml"}, "values": [["2500000000000000000", "middle"]]},
                ]
            ),
        )

    app = _app()
    with _patch_loki(handler):
        async with _client(app) as client:
            r = await client.get("/api/v1/logs/query?container=acme&search=error&since_minutes=30&limit=10")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 3
    assert [line["line"] for line in data["lines"]] == ["newest line", "middle", "older line"]
    assert data["lines"][0]["container"] == "acme-backend"
    assert seen["params"]["direction"] == "backward"
    assert seen["params"]["limit"] == "10"
    assert "acme" in seen["params"]["query"] and "error" in seen["params"]["query"]


async def test_raw_logql_overrides_built_query():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.params["query"]
        return httpx.Response(200, json=_loki_response([]))

    app = _app()
    with _patch_loki(handler):
        async with _client(app) as client:
            r = await client.get('/api/v1/logs/query?logql={job="x"} |= "y"&container=ignored')
    assert r.status_code == 200
    assert seen["query"] == '{job="x"} |= "y"'


async def test_loki_unreachable_returns_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    app = _app()
    with _patch_loki(handler):
        async with _client(app) as client:
            r = await client.get("/api/v1/logs/query")
    assert r.status_code == 502
    assert "cannot reach Loki" in r.json()["detail"]


async def test_unconfigured_loki_returns_503():
    app = _app(loki_url="")
    async with _client(app) as client:
        r = await client.get("/api/v1/logs/query")
    assert r.status_code == 503


async def test_limit_validation():
    app = _app()
    async with _client(app) as client:
        r = await client.get("/api/v1/logs/query?limit=5000")
    assert r.status_code == 422


# --- RAV-1295: multi-container alternation + container list endpoint ---


def test_build_query_containers_alternation():
    q = _build_query(None, None, "embry0-orchestrator, embry0-frontend")
    assert q == '{container_name=~"^(embry0\\\\-orchestrator|embry0\\\\-frontend)$"}'
    # containers wins over the substring param
    assert _build_query("ignored", None, "a") == '{container_name=~"^(a)$"}'
    # regex metacharacters in a name cannot widen the match
    assert ".*" not in _build_query(None, None, "evil.*name")


async def test_query_accepts_containers_param():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.params["query"]
        return httpx.Response(200, json=_loki_response([]))

    app = _app()
    with _patch_loki(handler):
        async with _client(app) as client:
            r = await client.get("/api/v1/logs/query?containers=embry0-orchestrator,embry0-frontend")
    assert r.status_code == 200
    assert seen["query"].startswith('{container_name=~"^(')


async def test_list_containers_proxies_label_values_and_caches():
    from embry0.api.v1 import logs as logs_module

    # Reset the module cache so this test is order-independent.
    logs_module._CONTAINERS_CACHE["at"] = 0.0
    logs_module._CONTAINERS_CACHE["values"] = []

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/loki/api/v1/label/container_name/values"
        return httpx.Response(200, json={"data": ["embry0-orchestrator", "embry0-frontend"]})

    app = _app()
    with _patch_loki(handler):
        async with _client(app) as client:
            r1 = await client.get("/api/v1/logs/containers")
            r2 = await client.get("/api/v1/logs/containers")
    assert r1.status_code == 200
    assert r1.json() == {"containers": ["embry0-frontend", "embry0-orchestrator"], "cached": False}
    assert r2.json()["cached"] is True
    assert calls["n"] == 1  # second hit served from the 60s cache

    logs_module._CONTAINERS_CACHE["at"] = 0.0
    logs_module._CONTAINERS_CACHE["values"] = []


async def test_list_containers_unconfigured_loki_503():
    app = _app(loki_url="")
    async with _client(app) as client:
        r = await client.get("/api/v1/logs/containers")
    assert r.status_code == 503
