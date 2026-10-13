"""A log corpus carrying one of every secret kind embry0 knows, plus PII (#294).

Shared by the redactor's unit tests and the watcher's end-to-end tests, so
"the briefing is redacted" and "the redactor knows this kind" are pinned
against the same lines.

Token values are assembled from pieces at import time: a literal that looks
like a live credential trips forge secret scanning on push. None of these is
a real credential.
"""

from __future__ import annotations

_GH = "ghp_" + "a1B2c3D4" * 5
_GH_PAT = "github_pat_" + "11ABCDEFG0" * 3
_GL = "glpat-" + "Zq9xY8wV7u6T5s4R"
_ANT = "sk-ant-oat01-" + "Zx9_" * 12
_XAI = "xai-" + "Q7rS8tU9" * 4
_LIN = "lin_api_" + "k3Lm4Np5Qr6St7Uv"
_SLACK = "xox" + "b-" + "1234567890-abcdefghij"
_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
_TG = "123456789:" + "AAH" + "x" * 16 + "Yz0123456789abcd"
_SLACK_HOOK = "T0000" + "AAAA/B0000BBBB/" + "c" * 24
_DIGEST = "0123456789abcdef" * 2 + "01234567"
_PEM_BODY = "MIIEowIBAAKCAQEA" + "q" * 20

#: (log line, the exact substring that must not survive redact_log_text).
CORPUS: list[tuple[str, str]] = [
    (f"git fetch failed: https://x-access-token:{_GH}@github.com/o/r.git", _GH),
    (f"remote said: bad credentials for {_GH_PAT}", _GH_PAT),
    (f"gitlab 401 with {_GL}", _GL),
    (f"anthropic rejected {_ANT}", _ANT),
    (f"xai key {_XAI} over quota", _XAI),
    (f"linear call with {_LIN} failed", _LIN),
    (f"slack post via {_SLACK} refused", _SLACK),
    (f"aws {_AWS} denied", _AWS),
    (f"GET /x Authorization: Bearer {_JWT}", _JWT),
    (f"jwt in body {_JWT}", _JWT),
    ("upstream: Proxy-Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
    ("X-Api-Key: 9f8e7d6c5b4a", "9f8e7d6c5b4a"),
    ("PRIVATE-TOKEN: plainvalue12", "plainvalue12"),
    ("Cookie: session=abcdef0099; theme=dark", "abcdef0099"),
    ('{"password": "hunter2hunter2", "user": "bob"}', "hunter2hunter2"),
    ("db_password=s3cr3t!x retrying", "s3cr3t!x"),
    ("client_secret: 'q1w2e3r4'", "q1w2e3r4"),
    ("refresh_token=1//0gabcdefg", "1//0gabcdefg"),
    ("aws_access_key=plainvalue34 rejected", "plainvalue34"),
    ("session_id=plainvalue56 expired", "plainvalue56"),
    ("postgresql://app:p%40ssw0rd@db:5432/app refused", "p%40ssw0rd"),
    (f"telegram https://api.telegram.org/bot{_TG}/sendMessage 404", _TG.split(":", 1)[1]),
    (f"slack webhook https://hooks.slack.com/services/{_SLACK_HOOK} 410", _SLACK_HOOK),
    (f"-----BEGIN RSA PRIVATE KEY-----\n{_PEM_BODY}\n-----END RSA PRIVATE KEY-----", _PEM_BODY),
    (f"digest {_DIGEST} mismatch", _DIGEST),
    ("mailbox jane.doe+x@example.com returned 403", "jane.doe+x@example.com"),
]

#: The e-mail every corpus consumer checks is replaced by the placeholder.
EMAIL = "jane.doe+x@example.com"
