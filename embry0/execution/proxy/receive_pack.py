"""The ref updates of a ``git-receive-pack`` request, read before its pack (#559, ADR 0009).

A push is one POST whose body is the command list, then the pack::

    update-request = *shallow command-list [push-options] [pack]
    shallow        = PKT-LINE("shallow" SP obj-id)
    command-list   = PKT-LINE(command NUL capability-list) *PKT-LINE(command) flush-pkt
    command        = old-id SP new-id SP refname

(git's ``Documentation/gitprotocol-pack.txt``). The command list is small and
comes first, so the git-proxy reads it, decides every ref update against the
branches the job may push, and then either forwards the bytes it read followed
by the rest of the body unchanged (push options and the pack, streamed, never
held), or answers a refusal git prints per ref.

Everything here is pure: :func:`read_command_list` takes any reader with
``readexactly`` (aiohttp's request body, or an :class:`asyncio.StreamReader` in
tests), and the rest works on bytes.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "MAX_COMMANDS",
    "CommandList",
    "MalformedCommandList",
    "RefUpdate",
    "read_command_list",
    "refused_updates",
    "refusal_response",
]

# LARGE_PACKET_MAX: the longest pkt-line git writes, length prefix included.
_MAX_PKT = 65520
# A push names a ref per command; the proxy allows one branch, so a long list
# is refused anyway. The cap bounds what is read before deciding.
MAX_COMMANDS = 1024
_MAX_SHALLOW = 4096
_PKT_LEN = re.compile(rb"^[0-9a-fA-F]{4}$")
_OID = re.compile(rb"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SIDEBAND_64K_DATA = 65515  # LARGE_PACKET_MAX - 4 (length) - 1 (band)
_SIDEBAND_DATA = 995  # 1000 - 4 - 1, the old side-band limit


class _Reader(Protocol):
    async def readexactly(self, n: int) -> bytes: ...


class MalformedCommandList(ValueError):
    """The body does not start with a command list the proxy can decide; the push is refused."""


@dataclass(frozen=True)
class RefUpdate:
    """One ``<old-id> <new-id> <refname>`` command."""

    old: str
    new: str
    ref: str

    @property
    def is_create(self) -> bool:
        return set(self.old) == {"0"}

    @property
    def is_delete(self) -> bool:
        return set(self.new) == {"0"}


@dataclass(frozen=True)
class CommandList:
    """The parsed command list and the exact bytes it was read from (re-sent upstream as they came)."""

    updates: tuple[RefUpdate, ...]
    capabilities: frozenset[str]
    raw: bytes


async def _read_pkt(reader: _Reader) -> tuple[bytes, bytes | None]:
    """One pkt-line: ``(raw bytes, payload)``; payload None for a flush-pkt."""
    try:
        head = await reader.readexactly(4)
    except asyncio.IncompleteReadError as exc:
        raise MalformedCommandList("the push ended inside its command list") from exc
    if not _PKT_LEN.fullmatch(head):  # int(..., 16) alone would take "+0a1", " 0a1" or "0x1"
        raise MalformedCommandList(f"bad pkt-line length {head!r}")
    length = int(head, 16)
    if length == 0:
        return head, None
    if length < 4 or length > _MAX_PKT:
        # 0001-0003 are protocol v2's delim and response-end, never in a push.
        raise MalformedCommandList(f"pkt-line length {length} is not valid in a push")
    try:
        payload = await reader.readexactly(length - 4)
    except asyncio.IncompleteReadError as exc:
        raise MalformedCommandList("the push ended inside a pkt-line") from exc
    return head + payload, payload


def _parse_command(line: bytes) -> RefUpdate:
    parts = line.split(b" ")
    if len(parts) != 3:
        raise MalformedCommandList(f"not a ref update command: {line[:200]!r}")
    old, new, ref = parts
    if not _OID.fullmatch(old) or not _OID.fullmatch(new) or len(old) != len(new):
        raise MalformedCommandList(f"bad object ids in command: {line[:200]!r}")
    try:
        name = ref.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedCommandList(f"ref name is not UTF-8: {ref[:200]!r}") from exc
    if not name or any(ord(c) < 0x20 or c == "\x7f" for c in name):
        raise MalformedCommandList(f"bad ref name {name[:200]!r}")
    return RefUpdate(old=old.decode("ascii"), new=new.decode("ascii"), ref=name)


async def read_command_list(reader: _Reader) -> CommandList:
    """Read shallow lines and the command list up to and including its flush-pkt.

    Raises :class:`MalformedCommandList` for anything that is not one, including
    a signed push (``push-cert``), which the proxy does not decide. A body that
    is only a flush-pkt (git's probe before a large push) is an empty list.
    """
    raw = bytearray()
    updates: list[RefUpdate] = []
    capabilities: frozenset[str] = frozenset()
    shallow = 0
    while True:
        chunk, payload = await _read_pkt(reader)
        raw += chunk
        if payload is None:
            return CommandList(updates=tuple(updates), capabilities=capabilities, raw=bytes(raw))
        if payload.endswith(b"\n"):
            payload = payload[:-1]
        if payload.startswith(b"shallow "):
            if updates:
                raise MalformedCommandList("a shallow line after the first command")
            if not _OID.fullmatch(payload[len(b"shallow ") :]):
                raise MalformedCommandList(f"bad shallow line {payload[:200]!r}")
            shallow += 1
            if shallow > _MAX_SHALLOW:
                raise MalformedCommandList(f"more than {_MAX_SHALLOW} shallow lines")
            continue
        if payload.startswith(b"push-cert"):
            raise MalformedCommandList("signed pushes (push-cert) are not supported through the git-proxy")
        if not updates:
            payload, _, caps = payload.partition(b"\0")
            capabilities = frozenset(caps.decode("ascii", "replace").split())
        elif b"\0" in payload:
            raise MalformedCommandList("a capability list after the first command")
        updates.append(_parse_command(payload))
        if len(updates) > MAX_COMMANDS:
            raise MalformedCommandList(f"more than {MAX_COMMANDS} ref updates in one push")


def refused_updates(updates: Collection[RefUpdate], allowed_branches: Collection[str]) -> dict[str, str]:
    """``{ref: reason}`` for every update the job may not make; empty means the push may go on.

    A job may create and update (fast-forward or not) ``refs/heads/<branch>``
    for each allowed branch. It may not delete one: no embry0 path deletes a
    branch, and deleting a pull request's head branch closes the pull request.
    Every other ref, tags included, is refused.
    """
    allowed = {f"refs/heads/{b}" for b in allowed_branches}
    refused: dict[str, str] = {}
    for update in updates:
        if update.ref not in allowed:
            refused[update.ref] = "not a branch this job may push"
        elif update.is_delete:
            refused[update.ref] = "deleting the job's branch is not allowed"
    return refused


def _pkt(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


def _band(band: int, data: bytes, size: int) -> bytes:
    return b"".join(_pkt(bytes([band]) + data[i : i + size]) for i in range(0, len(data), size))


def refusal_response(
    updates: Collection[RefUpdate], refused: Mapping[str, str], capabilities: Collection[str], message: str
) -> bytes | None:
    """A receive-pack result refusing every update, or None when git did not ask for a report.

    The whole push is refused: an update that would be allowed on its own is
    reported as refused because of the others, so a push never half-lands.
    ``message`` goes to git's stderr as ``remote: ...`` when the client asked
    for a side-band. Without ``report-status`` git cannot be told per ref and
    would read silence as success, so the caller refuses with an HTTP error.
    """
    if not {"report-status", "report-status-v2"} & set(capabilities):
        return None
    report = _pkt(b"unpack ok\n")
    for update in updates:
        reason = refused.get(update.ref, "refused with the rest of this push")
        report += _pkt(f"ng {update.ref} embry0 git-proxy: {reason}\n".encode())
    report += b"0000"
    if "side-band-64k" in capabilities:
        size = _SIDEBAND_64K_DATA
    elif "side-band" in capabilities:
        size = _SIDEBAND_DATA
    else:
        return report
    note = _band(2, f"embry0 git-proxy: {message}\n".encode(), size)
    return note + _band(1, report, size) + b"0000"
