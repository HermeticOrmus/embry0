"""The receive-pack command-list reader and the push refusal it feeds (#559)."""

from __future__ import annotations

import asyncio

import pytest

from embry0.execution.proxy.receive_pack import (
    MAX_COMMANDS,
    MalformedCommandList,
    RefUpdate,
    read_command_list,
    refusal_response,
    refused_updates,
)

ZERO = "0" * 40
A = "a" * 40
B = "b" * 40
CAPS = b"report-status side-band-64k quiet object-format=sha1 agent=git/2.47.0"


def pkt(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode() + payload


def body(*lines: bytes, rest: bytes = b"PACK-BYTES") -> bytes:
    return b"".join(pkt(line) for line in lines) + b"0000" + rest


async def read(data: bytes):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    parsed = await read_command_list(reader)
    return parsed, await reader.read()


async def test_reads_commands_and_capabilities_and_leaves_the_pack_unread():
    data = body(
        f"{ZERO} {A} refs/heads/job".encode() + b"\0" + CAPS + b"\n",
        f"{A} {B} refs/heads/main\n".encode(),
        f"{B} {ZERO} refs/tags/v1".encode(),
    )
    parsed, rest = await read(data)
    assert parsed.updates == (
        RefUpdate(ZERO, A, "refs/heads/job"),
        RefUpdate(A, B, "refs/heads/main"),
        RefUpdate(B, ZERO, "refs/tags/v1"),
    )
    assert {"report-status", "side-band-64k"} <= parsed.capabilities
    assert parsed.raw + rest == data, "the bytes read must be re-sent exactly, the pack after them untouched"
    assert rest == b"PACK-BYTES"
    assert parsed.updates[0].is_create and parsed.updates[2].is_delete and not parsed.updates[1].is_create


async def test_shallow_lines_before_the_commands_and_push_options_after_them():
    data = body(
        b"shallow " + B.encode() + b"\n",
        f"{A} {B} refs/heads/job".encode() + b"\0report-status push-options",
        rest=pkt(b"ci.skip") + b"0000" + b"PACK",
    )
    parsed, rest = await read(data)
    assert [u.ref for u in parsed.updates] == ["refs/heads/job"]
    assert rest == pkt(b"ci.skip") + b"0000PACK", "push options belong to the streamed remainder"


async def test_a_lone_flush_is_gits_probe_and_an_empty_list():
    parsed, rest = await read(b"0000")
    assert parsed.updates == () and parsed.raw == b"0000" and rest == b""


async def test_sha256_object_ids():
    z, a = "0" * 64, "c" * 64
    parsed, _ = await read(body(f"{z} {a} refs/heads/job".encode() + b"\0report-status"))
    assert parsed.updates[0].is_create


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty body"),
        pytest.param(b"00", id="truncated length"),
        pytest.param(b"zz10", id="non-hex length"),
        pytest.param(b"+0a1" + b"x" * 200, id="signed length"),
        pytest.param(b"0001", id="protocol v2 delim"),
        pytest.param(b"0003", id="length below header"),
        pytest.param(b"fff1" + b"x" * 70000, id="longer than LARGE_PACKET_MAX"),
        pytest.param(pkt(f"{ZERO} {A} refs/heads/job".encode())[:-3], id="truncated payload"),
        pytest.param(pkt(f"{ZERO} {A} refs/heads/job".encode()), id="no flush"),
        pytest.param(body(f"{ZERO} {A}".encode()), id="two fields"),
        pytest.param(body(f"{ZERO} {A} refs/heads/a b".encode()), id="four fields"),
        pytest.param(body(f"{ZERO[:-1]} {A} refs/heads/job".encode()), id="short old id"),
        pytest.param(body(f"{ZERO} {'A' * 40} refs/heads/job".encode()), id="upper-case id"),
        pytest.param(body(f"{ZERO} {'c' * 64} refs/heads/job".encode()), id="mixed id lengths"),
        pytest.param(body(f"{ZERO} {A} ".encode()), id="empty ref"),
        pytest.param(body(f"{ZERO} {A} refs/heads/\x01".encode()), id="control char in ref"),
        pytest.param(body(f"{ZERO} {A} ".encode() + b"refs/heads/\xff"), id="ref not utf-8"),
        pytest.param(
            body(f"{ZERO} {A} refs/heads/a".encode() + b"\0caps", f"{ZERO} {A} refs/heads/b".encode() + b"\0x"),
            id="caps on a later command",
        ),
        pytest.param(
            body(f"{ZERO} {A} refs/heads/a".encode() + b"\0caps", b"shallow " + A.encode()),
            id="shallow after a command",
        ),
        pytest.param(body(b"shallow nothex"), id="bad shallow"),
        pytest.param(body(b"push-cert\0report-status\n", b"certificate version 0.1\n"), id="signed push"),
    ],
)
async def test_malformed_input_is_refused(data):
    with pytest.raises(MalformedCommandList):
        await read(data)


async def test_too_many_commands_is_refused():
    lines = [f"{ZERO} {A} refs/heads/b{i}".encode() for i in range(MAX_COMMANDS + 1)]
    lines[0] += b"\0report-status"
    with pytest.raises(MalformedCommandList, match="ref updates"):
        await read(body(*lines))


def test_only_the_allowed_branch_may_be_created_or_updated_and_nothing_deleted():
    updates = [
        RefUpdate(ZERO, A, "refs/heads/job"),
        RefUpdate(A, B, "refs/heads/job"),
        RefUpdate(A, B, "refs/heads/main"),
        RefUpdate(ZERO, A, "refs/heads/other"),
        RefUpdate(ZERO, A, "refs/tags/job"),
        RefUpdate(ZERO, A, "job"),
        RefUpdate(A, ZERO, "refs/heads/job2"),
    ]
    refused = refused_updates(updates, {"job", "job2"})
    assert set(refused) == {"refs/heads/main", "refs/heads/other", "refs/tags/job", "job", "refs/heads/job2"}
    assert refused["refs/heads/job2"] == "deleting the job's branch is not allowed"
    assert refused_updates(updates[:2], {"job"}) == {}
    assert set(refused_updates(updates[:2], set())) == {"refs/heads/job"}, "no branch means no push"


def _demux(data: bytes) -> tuple[bytes, bytes]:
    """Split a side-band response into (band 1, band 2), checking every pkt-line on the way."""
    bands = {1: b"", 2: b""}
    i = 0
    while True:
        length = int(data[i : i + 4], 16)
        if length == 0:
            assert i + 4 == len(data), "bytes after the closing flush"
            return bands[1], bands[2]
        payload = data[i + 4 : i + length]
        bands[payload[0]] += payload[1:]
        i += length


def test_the_refusal_is_a_report_git_prints_per_ref():
    updates = [RefUpdate(ZERO, A, "refs/heads/job"), RefUpdate(A, B, "refs/heads/main")]
    refused = {"refs/heads/main": "not a branch this job may push"}
    out = refusal_response(updates, refused, {"report-status", "side-band-64k"}, "push refused")
    assert out is not None
    report, progress = _demux(out)
    assert progress == b"embry0 git-proxy: push refused\n"
    assert report == (
        pkt(b"unpack ok\n")
        + pkt(b"ng refs/heads/job embry0 git-proxy: refused with the rest of this push\n")
        + pkt(b"ng refs/heads/main embry0 git-proxy: not a branch this job may push\n")
        + b"0000"
    )
    plain = refusal_response(updates, refused, {"report-status-v2"}, "push refused")
    assert plain == report, "without a side-band the report is sent bare"


def test_no_report_is_possible_without_report_status():
    assert refusal_response([RefUpdate(A, B, "refs/heads/main")], {}, {"side-band-64k"}, "x") is None
