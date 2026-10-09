"""#573: the QA bucket is prepared in the background, retrying until MinIO
answers, and the clients are published only once it has. No MinIO needed."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from embry0.execution.qa import minio_client as minio_client_mod
from embry0.execution.qa.minio_client import QAMinioClient


def _client() -> QAMinioClient:
    # Constructing the client opens no connection.
    return QAMinioClient("minio:9000", "user", "password")


@pytest.mark.asyncio
async def test_prepare_bucket_retries_until_minio_answers(monkeypatch):
    client = _client()
    client.ensure_bucket = AsyncMock(side_effect=[ConnectionError("refused")] * 4 + [None])
    client.set_lifecycle_policy = AsyncMock()
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(minio_client_mod.asyncio, "sleep", fake_sleep)

    await client.prepare_bucket("qa-artifacts", expire_days=14, retry_delays=(1, 2, 5))

    assert client.ensure_bucket.await_count == 5, "it must keep trying past the first failure"
    assert sleeps == [1, 2, 5, 5], f"the last delay repeats once the list runs out, got {sleeps}"
    client.set_lifecycle_policy.assert_awaited_once_with("qa-artifacts", expire_days=14)


@pytest.mark.asyncio
async def test_prepare_bucket_retries_a_failed_lifecycle_too(monkeypatch):
    client = _client()
    client.ensure_bucket = AsyncMock()
    client.set_lifecycle_policy = AsyncMock(side_effect=[RuntimeError("503"), None])
    monkeypatch.setattr(minio_client_mod.asyncio, "sleep", AsyncMock())

    await client.prepare_bucket("qa-artifacts", expire_days=7, retry_delays=(0,))

    assert client.set_lifecycle_policy.await_count == 2, "a bucket without its lifecycle is not ready"


@pytest.mark.asyncio
async def test_clients_are_published_only_once_the_bucket_is_ready():
    from embry0.api.app import _bring_up_qa_minio

    ready = asyncio.Event()
    internal, sandbox = _client(), _client()

    async def prepare_bucket(bucket: str, *, expire_days: int, retry_delays) -> None:
        assert (bucket, expire_days) == ("qa-artifacts", 30)
        await ready.wait()

    internal.prepare_bucket = prepare_bucket
    executor = SimpleNamespace(_qa_minio=None, _qa_minio_sandbox=None)
    app = SimpleNamespace(state=SimpleNamespace(qa_minio=None, qa_minio_sandbox=None, issue_executor=executor))

    task = asyncio.create_task(_bring_up_qa_minio(app, internal, sandbox, expire_days=30))
    await asyncio.sleep(0)
    assert app.state.qa_minio is None, "published before the bucket was ready: QA routes would 500, not 503"
    assert executor._qa_minio is None

    ready.set()
    await task

    assert app.state.qa_minio is internal and app.state.qa_minio_sandbox is sandbox
    assert executor._qa_minio is internal and executor._qa_minio_sandbox is sandbox, (
        "an executor built before MinIO answered must get the clients too, or QA jobs fail until a restart"
    )


@pytest.mark.asyncio
async def test_publishing_before_the_executor_exists():
    from embry0.api.app import _bring_up_qa_minio

    internal, sandbox = _client(), _client()
    internal.prepare_bucket = AsyncMock()
    app = SimpleNamespace(state=SimpleNamespace(qa_minio=None, qa_minio_sandbox=None))

    await _bring_up_qa_minio(app, internal, sandbox, expire_days=30)

    assert app.state.qa_minio is internal and app.state.qa_minio_sandbox is sandbox
