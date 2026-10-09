"""The JVM dev image has to run both stacks a monorepo dev check can ask for."""

from pathlib import Path

import embry0


def test_jvm_dev_image_installs_poetry() -> None:
    root = Path(embry0.__file__).resolve().parents[1]
    text = (root / "infra" / "Dockerfile.sandbox").read_text()
    stage = text.split("FROM base AS dev-jvm-python", 1)[1].split("\nFROM ", 1)[0]
    assert "poetry==1.8.4" in stage, "the jvm dev image must be able to run Poetry checks"
