"""The orchestrator image's baked env must not trip the orchestrator (#334).

The image sets env so the in-container CLI reaches the API. Anything it sets
under an embry0 prefix is, by construction, not a typo — but the startup
unknown-env check does not know that, and a permanent false positive there
destroys the signal that exists to catch real typos (#338 review).
"""

import re
from pathlib import Path

from embry0.config import unknown_prefixed_env_keys

DOCKERFILE = Path(__file__).resolve().parents[2] / "infra" / "Dockerfile.orchestrator"


def _baked_env() -> dict[str, str]:
    """ENV key=value pairs the image sets, honouring backslash continuations."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    env: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("ENV "):
            env.update(dict(re.findall(r"([A-Z_][A-Z0-9_]*)=(\S+)", line)))
    return env


def test_image_points_the_cli_at_the_in_container_api_port():
    env = _baked_env()
    assert env["EMBRY0_API_URL"] == "http://localhost:8000"
    # `embry0 health` derives its URL from PROD_PORT.
    assert env["PROD_PORT"] == "8000"


def test_image_does_not_set_embry0_url():
    """It is also the base for the console/board deep links linear_dispatch
    prints, and :8000 serves no UI route — those links would 404."""
    assert "EMBRY0_URL" not in _baked_env()


def test_no_baked_env_key_reads_as_an_unknown_config_key():
    assert unknown_prefixed_env_keys(_baked_env()) == []
