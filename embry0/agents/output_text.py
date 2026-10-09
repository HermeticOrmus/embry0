"""Assembly of an agent run's final text — shared by both executors (#329).

An agent's deliverable is parsed downstream by line-anchored contracts
(``TITLE:``, ``NO_ISSUE``, ``VERDICT:``, a JSON decision). The executors used
to concatenate every assistant message with no separator, so a contract line
that opened a new message arrived glued to the last word of the previous
message's narration ("...existing tickets.TITLE: Fix") and was lost.

Rules:

* text blocks WITHIN one assistant message are one continuous text and are
  concatenated as-is — inserting a break there could split a sentence;
* consecutive MESSAGES are joined with a blank line;
* the size cap is applied AFTER joining, and the cut is moved forward to the
  next line start when one is close, so a parser never sees half a first line.

Stdlib only: this module is baked into the sandbox images.
"""

from __future__ import annotations

MESSAGE_SEPARATOR = "\n\n"
MAX_OUTPUT_CHARS = 10_000

# How far into the kept window the cut may move to reach a line start. Bounded
# so one late newline in a wall of text cannot discard most of the tail.
_SNAP_FRACTION = 5


def tail_on_line_boundary(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """The last ``limit`` characters of ``text``, starting on a line when possible.

    Never returns more than ``limit`` characters. Falls back to the raw tail
    when the window holds no line start within its first 1/5th.
    """
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    if text[cut - 1] == "\n":
        return text[cut:]
    newline = text.find("\n", cut, cut + limit // _SNAP_FRACTION)
    if newline == -1:
        return text[cut:]
    return text[newline + 1 :]


class FinalText:
    """Accumulates assistant messages into the run's final text."""

    def __init__(self) -> None:
        self._messages: list[str] = []

    def add_message(self, *blocks: str) -> None:
        """Record one assistant message from its text blocks, in order.

        A message with no visible text (tool-use / thinking only) is skipped so
        it contributes no separator.
        """
        message = "".join(blocks)
        if message.strip():
            self._messages.append(message)

    def __bool__(self) -> bool:
        return bool(self._messages)

    def render(self, limit: int = MAX_OUTPUT_CHARS) -> str:
        return tail_on_line_boundary(MESSAGE_SEPARATOR.join(self._messages), limit)
