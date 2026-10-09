"""The orchestrator image must ship embry0_mcp — the hosted MCP service
(compose `mcp`, console script embry0-mcp-http) runs from that image.

Regression: the entry point installed from pyproject but the module was
absent from the /app source copy, so the service crash-looped with
ModuleNotFoundError at first deploy of #224.
"""

from pathlib import Path

_DOCKERFILE = Path(__file__).parents[3] / "infra" / "Dockerfile.orchestrator"


def test_orchestrator_dockerfile_copies_embry0_mcp() -> None:
    content = _DOCKERFILE.read_text()
    assert "COPY embry0_mcp/ /app/embry0_mcp/" in content
