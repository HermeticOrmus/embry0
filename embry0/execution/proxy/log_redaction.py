"""Best-effort redaction of secrets and PII in log text (SH-P4.4).

Log lines handed to an agent end up in a prompt, and prompts go to third-party
model providers. This is the narrow first slice that the developer's log tool
and the developer/reviewer evidence blocks need; the full, configurable module
with a corpus is issue #294, which should absorb or replace this file.

CONVERGENCE NOTE for #294 — ``embry0/safety/redaction.py`` (#281) landed while
this branch was in review and is the better home. It arrived independently at
both of this module's load-bearing ideas: bounded repetition to keep matching
linear, and redacting a generous window BEFORE capping so a cut cannot leave
half a token behind (its ``redacted_stderr_tail`` even drops a partial first
word, which ``redact_bounded`` below does not). Neither module is a superset:
that one also covers PEM blocks, the runner's ``--config`` payload and the
process's own secret env values; this one also covers e-mail addresses and any
long opaque run, which is what LOG lines need and diagnostics do not. The
convergence is to port those two rule classes into ``embry0/safety`` and delete
this file — it is deliberately not done here because that module is already on
the agent-stderr path, and because ``infra/Dockerfile.proxy`` copies modules
one by one and would need ``embry0/safety/`` added to the proxy image.

Deliberately over-eager: a redacted commit SHA costs a developer little, a
leaked key costs a rotation. It is regex-based and therefore NOT a guarantee —
callers must still bound what they pass on.

Stdlib only: the module ships in the proxy image (see infra/Dockerfile.proxy).
"""

from __future__ import annotations

import re
from collections.abc import Callable

REDACTED = "[REDACTED]"
_EMAIL = "<email>"

# The prefix/suffix runs are BOUNDED. Unbounded `[\w.-]*` around the alternation
# backtracks quadratically on one long unbroken token (32k chars took 44s), and
# every caller feeds this data it does not control.
_SECRET_KEY = (
    r"[\w.-]{0,40}(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|credential|session[_-]?id|signature)[\w.-]{0,40}"
)

# How much text beyond a caller's limit the redactor still sees, so a credential
# straddling the cut is matched whole rather than as an unrecognizable fragment.
REDACT_OVERLAP_CHARS = 256

_RULES: list[tuple[re.Pattern[str], str]] = [
    # Credential-bearing headers: the value runs to the end of the line.
    (
        re.compile(r"(?i)\b((?:proxy-)?authorization|x-api-key|x-auth-token|cookie|set-cookie)(\s*[:=]\s*)[^\r\n]+"),
        rf"\1\2{REDACTED}",
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {REDACTED}"),
    # "key": "value" / key='value' / key=value for secret-looking key names.
    (re.compile(rf"(?i)([\"']?{_SECRET_KEY}[\"']?\s*[:=]\s*)([\"'])(?:(?!\2).)*\2"), rf"\1\g<2>{REDACTED}\2"),
    (re.compile(rf"(?i)({_SECRET_KEY}\s*[:=]\s*)(?![\"'\[])[^\s,;&\"'}}\])]+"), rf"\1{REDACTED}"),
    # scheme://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"), rf"\1{REDACTED}@"),
    # JWTs and well-known token prefixes.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), REDACTED),
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{12,}|xai-[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"glpat-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|lin_api_[A-Za-z0-9]{16,})"
        ),
        REDACTED,
    ),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"), _EMAIL),
    # Any remaining long opaque run (hex digests, base64 keys, session ids).
    (re.compile(r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*\d)[A-Za-z0-9+/_-]{32,}={0,2}"), REDACTED),
]


def redact_log_text(text: str) -> str:
    """``text`` with secret-looking and personal values replaced. Idempotent."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_bounded(
    text: str,
    limit: int,
    *,
    redact: Callable[[str], str] = redact_log_text,
) -> str:
    """Redact and cut to ``limit``, with the redactor seeing bounded input.

    Callers bound log text before it reaches a prompt or a response. Redacting
    the whole thing first makes the cost depend on input the caller does not
    control; cutting first can hand the redactor half a credential. So the
    window is ``limit`` plus an overlap wide enough for a straddling token, and
    the result is cut again afterwards — a cut landing inside a ``[REDACTED]``
    marker leaves a harmless fragment of the marker, not of the secret.
    """
    if limit <= 0:
        return ""
    return redact(text[: limit + REDACT_OVERLAP_CHARS])[:limit]
