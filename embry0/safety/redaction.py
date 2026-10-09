"""Free-text secret redaction for diagnostics that leave the process.

``docker_client._scrub_cmd_for_log`` covers ARGV (``-e KEY=VAL`` values and
the ``--config`` payload). Agent stderr is free text — a traceback, a CLI
error, a curl dump — so it needs a text scrubber: nothing covered tokens or
auth headers before #281.

Default-deny where the shape allows it: any ``KEY=value`` / ``"key": "value"``
whose key NAME looks secret is redacted whatever the value looks like, the way
the argv scrubber treats every ``-e`` value. Known token formats and the
process's own secret env values are redacted wherever they appear.

Pure and dependency-free: this module is baked into the sandbox images.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

REDACTED = "***REDACTED***"

# Storing a diagnostic tail must never become a way to store a log.
STDERR_TAIL_MAX_BYTES = 8 * 1024

_SECRET_KEY_NAME = r"[A-Za-z0-9_.\-]*(?:token|secret|passw(?:or)?d|api[_\-]?key|apikey|credential|private[_\-]?key|authorization|cookie|session[_\-]?key)[A-Za-z0-9_.\-]*"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM blocks first — they span lines.
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL),
        REDACTED,
    ),
    # The runner's --config payload carries prompts and per-sandbox bearers.
    (re.compile(r"(--config(?:-json)?[= ]+)[^\n]+"), rf"\1{REDACTED}"),
    # HTTP auth headers, any scheme: "Authorization: Bearer x", "x-api-key: y".
    (
        re.compile(
            r"(?i)\b((?:proxy-)?authorization|x-api-key|x-auth-token|private-token|cookie|set-cookie)(\s*[:=]\s*)[^\r\n]+"
        ),
        rf"\1\2{REDACTED}",
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), rf"\1 {REDACTED}"),
    # Credentials in a URL: scheme://user:pass@host. Every repetition is
    # BOUNDED: with an open-ended scheme class the engine rescans the whole
    # remaining text from each starting position looking for "://", which is
    # quadratic — a 200 KiB unbroken word (a base64 blob in a traceback is
    # exactly that) took 16s. A scheme is a handful of characters and neither
    # a userinfo nor a password runs to 256.
    (re.compile(r"([A-Za-z][A-Za-z0-9+.\-]{0,31}://)[^/\s:@]{1,256}:[^/\s@]{1,256}@"), rf"\1{REDACTED}@"),
    # KEY=value / KEY: value / "key": "value" with a secret-looking key name.
    (
        re.compile(rf"(?i)([\"']?\b{_SECRET_KEY_NAME}[\"']?\s*[:=]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;&\"']+)"),
        rf"\1{REDACTED}",
    ),
    # Known token formats, wherever they turn up.
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\b(?:sk|xai|rk)-[A-Za-z0-9_\-]{20,}"), REDACTED),
    (re.compile(r"\blin_(?:api|oauth)_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), REDACTED),
)

_SECRET_ENV_NAME = re.compile(r"(?i)token|secret|passw|api_?key|credential|private_?key|database_url|dsn")
# A short value ("true", "1", "dev") would redact ordinary words all over the text.
_MIN_KNOWN_VALUE_LEN = 8

# Spec 2026-09-29 §5: secrets that never entered the environment (the
# credential registry) still have to be scrubbed. app.py registers them at
# startup; the credentials API re-registers after create/rotate.
_KNOWN_SECRETS: set[str] = set()


def register_known_secrets(values: Iterable[str], *, reset: bool = False) -> None:
    if reset:
        _KNOWN_SECRETS.clear()
    _KNOWN_SECRETS.update(v for v in values if isinstance(v, str) and len(v) >= _MIN_KNOWN_VALUE_LEN)


def known_secret_values() -> list[str]:
    return sorted(_KNOWN_SECRETS)


def secret_env_values(environ: dict[str, str] | None = None) -> list[str]:
    """Values of this process's secret-looking env vars plus the registered
    known secrets, longest first, so a secret that appears verbatim is caught
    even in a shape no pattern knows."""
    env = os.environ if environ is None else environ
    values = {v for k, v in env.items() if _SECRET_ENV_NAME.search(k) and len(v) >= _MIN_KNOWN_VALUE_LEN}
    return sorted(values | _KNOWN_SECRETS, key=len, reverse=True)


def redact_secrets(text: str, known_values: Iterable[str] = ()) -> str:
    """``text`` with secrets replaced by ``***REDACTED***``. Exact known
    values go first (a pattern could otherwise leave half of one behind)."""
    if not text:
        return text
    for value in known_values:
        if value and len(value) >= _MIN_KNOWN_VALUE_LEN:
            text = text.replace(value, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def tail_bytes(text: str, max_bytes: int = STDERR_TAIL_MAX_BYTES) -> str:
    """The last ``max_bytes`` of ``text`` (UTF-8), cut on a character
    boundary and, when it fits, on a line boundary. Diagnostics are prose:
    truncating them is fine, and the newest lines are the ones that explain
    an exit."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    cut = raw[-max_bytes:].decode("utf-8", errors="ignore")
    newline = cut.find("\n")
    if 0 <= newline < len(cut) - 1:
        cut = cut[newline + 1 :]
    return cut


LOG_FIELD_MAX_CHARS = 2000


def redacted_log_field(
    text: str | None,
    known_values: Iterable[str] | None = None,
    *,
    limit: int = LOG_FIELD_MAX_CHARS,
) -> str:
    """A free-text log field: secrets removed, then length-capped.

    Redact before capping. A cap that sliced a token in half would leave a
    fragment no pattern recognises, and that fragment is what would ship.
    """
    if not text:
        return ""
    values = secret_env_values() if known_values is None else list(known_values)
    return redact_secrets(text, values)[:limit]


def redacted_stderr_tail(stderr: str, known_values: Iterable[str] | None = None) -> str:
    """What may be STORED of an agent's stderr: redact the generous tail
    first, then cap — capping first could slice a token in half and leave a
    fragment no pattern recognises."""
    if not stderr or not stderr.strip():
        return ""
    values = secret_env_values() if known_values is None else list(known_values)
    generous = tail_bytes(stderr, STDERR_TAIL_MAX_BYTES * 4)
    dropped = stderr[: len(stderr) - len(generous)]
    if dropped and not dropped.endswith("\n"):
        # The window opened mid-line, possibly mid-token: a token's back half
        # matches no pattern, so the partial first word goes.
        generous = re.sub(r"^\S+", "", generous)
    return tail_bytes(redact_secrets(generous, values), STDERR_TAIL_MAX_BYTES).strip()
