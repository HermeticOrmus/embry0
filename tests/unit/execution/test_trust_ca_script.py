"""The sandbox CA helper ships in both sandbox images (spec 1A §3.2).

The script is what makes SandboxManager._install_ca_bundle work; an image
missing it fails at clone time inside DinD, a long way from the change that
dropped the COPY line.
"""

from pathlib import Path

from embry0.execution.tls import SANDBOX_CA_BUNDLE_PATH

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "infra" / "scripts" / "embry0-trust-ca"


def test_the_script_exists_and_is_executable() -> None:
    assert _SCRIPT.is_file()
    assert _SCRIPT.stat().st_mode & 0o111, "embry0-trust-ca must be +x in git"


def test_the_script_writes_the_path_python_expects() -> None:
    """One constant, two languages — they must not drift."""
    assert SANDBOX_CA_BUNDLE_PATH.endswith("/.embry0/ca-bundle.pem")
    assert ".embry0" in _SCRIPT.read_text()
    assert "ca-bundle.pem" in _SCRIPT.read_text()


def test_both_sandbox_images_install_it() -> None:
    for name in ("Dockerfile.sandbox", "Dockerfile.sandbox.qa"):
        text = (_REPO_ROOT / "infra" / name).read_text()
        assert "infra/scripts/embry0-trust-ca" in text, f"{name} does not COPY embry0-trust-ca"
        assert "/usr/local/bin/embry0-trust-ca" in text, f"{name} does not chmod embry0-trust-ca"
