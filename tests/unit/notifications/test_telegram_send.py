"""Telegram outbound send — retry of transient failures (#252).

A blocking question that never reaches the chat leaves the job in
``awaiting_input`` with nothing for the operator to answer, so a 503 or a
dropped connection buys another attempt. A 4xx does not: a bad token or a
bad chat_id fails identically next time.
"""

import json

import httpx

from embry0.notifications.telegram import send_message

TOKEN = "bot-token"
CHAT = "chat-1"


class _Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _client(responses):
    """Serves the given responses in order; the last one repeats."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.seen = seen  # type: ignore[attr-defined]
    return client


def _ok(message_id: int = 42) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})


def _error(
    status: int,
    headers: dict[str, str] | None = None,
    *,
    retry_after: int | None = None,
) -> httpx.Response:
    body: dict = {"ok": False, "description": "nope"}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return httpx.Response(status, json=body, headers=headers or {})


async def test_send_delivers_once_after_two_transient_failures():
    client = _client([_error(503), _error(503), _ok()])
    sleeps = _Sleeps()

    assert await send_message(TOKEN, CHAT, "Q?", http_client=client, sleep=sleeps) == 42
    assert len(client.seen) == 3
    assert len(sleeps.delays) == 2


async def test_send_does_not_retry_a_permanent_rejection():
    client = _client([_error(400)])
    sleeps = _Sleeps()

    assert await send_message(TOKEN, CHAT, "Q?", http_client=client, sleep=sleeps) is None
    assert len(client.seen) == 1
    assert sleeps.delays == []


async def test_send_honours_retry_after_on_429():
    client = _client([_error(429, {"Retry-After": "2"}), _ok()])
    sleeps = _Sleeps()

    assert await send_message(TOKEN, CHAT, "Q?", http_client=client, sleep=sleeps) == 42
    assert sleeps.delays == [2.0]


async def test_send_gives_up_after_the_attempt_budget():
    client = _client([_error(503)])

    assert await send_message(TOKEN, CHAT, "Q?", http_client=client, sleep=_Sleeps()) is None
    assert len(client.seen) == 3


async def test_send_retries_a_dropped_connection():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("telegram unreachable")
        return _ok()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await send_message(TOKEN, CHAT, "Q?", http_client=client, sleep=_Sleeps()) == 42
    assert len(calls) == 2


async def test_send_never_raises_when_the_transport_stays_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("telegram unreachable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await send_message(TOKEN, CHAT, "Q?", http_client=client, sleep=_Sleeps()) is None


async def test_send_does_not_retry_a_200_that_is_not_ok():
    """Telegram reports logical errors as 200 + ``ok: false`` — retrying the
    same malformed request just repeats it."""
    client = _client([httpx.Response(200, json={"ok": False, "description": "chat not found"})])

    assert await send_message(TOKEN, CHAT, "Q?", http_client=client, sleep=_Sleeps()) is None
    assert len(client.seen) == 1


async def test_send_posts_the_truncated_text_without_parse_mode():
    client = _client([_ok()])
    await send_message(TOKEN, CHAT, "x" * 5000, http_client=client, sleep=_Sleeps())

    request = client.seen[0]
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    payload = json.loads(request.content)
    assert payload == {"chat_id": CHAT, "text": "x" * 4000}


async def test_send_honours_flood_control_from_the_response_body():
    """Telegram's authoritative delay is ``parameters.retry_after`` in the
    JSON body; the header is optional, and the backoff curve (1s, 2s) sits
    well inside the flood window."""
    client = _client([_error(429, retry_after=5), _ok()])
    sleeps = _Sleeps()

    assert await send_message(TOKEN, CHAT, "Q?", http_client=client, sleep=sleeps) == 42
    assert sleeps.delays == [5.0]


async def test_send_caps_an_absurd_flood_control_delay():
    client = _client([_error(429, retry_after=3600), _ok()])
    sleeps = _Sleeps()

    assert await send_message(TOKEN, CHAT, "Q?", http_client=client, sleep=sleeps) == 42
    assert sleeps.delays == [30.0]


async def test_send_retries_a_200_not_ok_carrying_flood_control():
    """Flood control also arrives as ``200`` + ``ok: false`` — transient
    despite the status the plain ``ok: false`` path treats as permanent."""
    client = _client([httpx.Response(200, json={"ok": False, "parameters": {"retry_after": 3}}), _ok()])
    sleeps = _Sleeps()

    assert await send_message(TOKEN, CHAT, "Q?", http_client=client, sleep=sleeps) == 42
    assert len(client.seen) == 2
    assert sleeps.delays == [3.0]
