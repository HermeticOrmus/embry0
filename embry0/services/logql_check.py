"""Ask Loki whether a LogQL query parses (#270).

Loki is the parser of record — there is no second LogQL grammar here to drift
from it. The probe is a one-line range query over the last minute, so it costs
nothing and returns the same ``400 parse error`` a real tick would get.

The one distinction that matters to the caller is *whose fault a failure is*:

- ``INVALID``       — Loki answered and refused the query (400, or 422). The
  query is wrong and will stay wrong until an operator edits it. This is the
  state a backslash-escaped ``WATCHER_LOGQL`` produced for nine silent days.
- ``MISCONFIGURED`` — something answered, but not about the query: any other
  4xx (401/403 auth, 404 wrong path or not a Loki at all — Prometheus answers
  404 here). The fix is the URL or credentials, never the query.
- ``MISSING``       — there is no query to probe. Never returned by
  :func:`check_logql`; callers use it for an unset setting.
- ``UNREACHABLE``   — no usable answer (connection error, timeout, 429, 5xx).
  Says nothing about the query; the caller should retry later, never crash.
"""

import time
from dataclasses import dataclass
from enum import StrEnum

import httpx

_PROBE_WINDOW_SECONDS = 60
_PROBE_TIMEOUT_SECONDS = 10.0
_DETAIL_CHARS = 500
# The only statuses that are a verdict on the query itself.
_QUERY_REJECTED = frozenset({400, 422})


class LogQLStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    MISCONFIGURED = "misconfigured"
    MISSING = "missing"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class LogQLCheck:
    status: LogQLStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is LogQLStatus.OK


async def check_logql(loki_url: str, logql: str, *, http_client: httpx.AsyncClient | None = None) -> LogQLCheck:
    """Probe ``logql`` against Loki. Never raises for a Loki/network failure."""
    end = time.time_ns()
    params = {
        "query": logql,
        "start": str(end - _PROBE_WINDOW_SECONDS * 1_000_000_000),
        "end": str(end),
        "limit": "1",
    }
    client = http_client or httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS)
    try:
        resp = await client.get(f"{loki_url.rstrip('/')}/loki/api/v1/query_range", params=params)
    except httpx.HTTPError as exc:
        return LogQLCheck(LogQLStatus.UNREACHABLE, f"{type(exc).__name__}: {exc}"[:_DETAIL_CHARS])
    finally:
        if http_client is None:
            await client.aclose()

    if resp.is_success:
        return LogQLCheck(LogQLStatus.OK)
    detail = f"HTTP {resp.status_code}: {resp.text.strip()}"[:_DETAIL_CHARS]
    if resp.status_code in _QUERY_REJECTED:
        return LogQLCheck(LogQLStatus.INVALID, detail)
    # 429 is Loki throttling us: transient, like a 5xx.
    if resp.is_client_error and resp.status_code != 429:
        return LogQLCheck(LogQLStatus.MISCONFIGURED, detail)
    return LogQLCheck(LogQLStatus.UNREACHABLE, detail)
