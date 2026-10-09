"""Guard: every builtin profile's base_image is actually built and pushed,
and the CLI builds every image the push needs.

A profile naming an image that `init-push-images` does not push is silent
here and fatal at orchestrator startup — the push script exits non-zero when
a tag is missing from the host daemon, which takes the stack down. These
tests make that wiring mistake fail in CI instead.
"""

import re
from pathlib import Path

import yaml

from embry0.cli import __main__ as cli
from embry0.storage.seeds.sandbox_profiles_builtin import BUILTIN_SANDBOX_PROFILES

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "infra" / "docker-compose.yml"
PUSH_SCRIPT = REPO_ROOT / "infra" / "scripts" / "push-images-to-registry.sh"


def _profile_images() -> set[str]:
    """Image names (tag stripped) referenced by builtin profiles."""
    return {fields["base_image"].split(":", 1)[0] for fields in BUILTIN_SANDBOX_PROFILES.values()}


def test_every_profile_image_is_in_the_push_list():
    images_line = next(line for line in PUSH_SCRIPT.read_text().splitlines() if line.startswith("IMAGES="))
    pushed = set(re.findall(r"embry0-[a-z0-9.-]+", images_line))
    missing = _profile_images() - pushed
    assert not missing, (
        f"builtin profiles reference images that push-images-to-registry.sh never pushes: "
        f"{sorted(missing)}. init-push-images exits non-zero on a missing tag, which fails "
        f"orchestrator startup. Add them to the IMAGES default in {PUSH_SCRIPT}."
    )


def test_every_profile_image_has_a_compose_builder():
    compose = COMPOSE.read_text()
    missing = [img for img in sorted(_profile_images()) if f"image: {img}:latest" not in compose]
    assert not missing, (
        f"builtin profiles reference images with no compose builder service: {missing}. "
        f"Nothing builds them, so `docker compose --profile images build` cannot produce "
        f"the tag that init-push-images then demands. Add a *-image-builder service in {COMPOSE}."
    )


# ── what `embry0 start` / `embry0 build` build (#531) ────────────────────────


def _compose_services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _pushed_images() -> set[str]:
    images_line = next(line for line in PUSH_SCRIPT.read_text().splitlines() if line.startswith("IMAGES="))
    return set(re.findall(r"embry0-[a-z0-9.-]+", images_line))


def test_the_cli_builds_every_service_compose_can_build():
    buildable = {name for name, svc in _compose_services().items() if "build" in svc}
    built = set(getattr(cli, "BUILD_SERVICES", ()))
    assert built == buildable, (
        f"embry0 start/build build {sorted(built)}, but the compose services with a build are "
        f"{sorted(buildable)}. Missing: {sorted(buildable - built)}; not buildable: "
        f"{sorted(built - buildable)}. Keep BUILD_SERVICES in embry0/cli/__main__.py equal to them."
    )


def test_the_cli_builds_every_image_init_push_images_pushes():
    services = _compose_services()
    builders = getattr(cli, "IMAGE_BUILDER_SERVICES", ())
    built = {services[name].get("image", "").split(":", 1)[0] for name in builders if name in services}
    missing = _pushed_images() - built
    assert not missing, (
        f"init-push-images pushes {sorted(missing)}, which no service in the CLI's "
        f"IMAGE_BUILDER_SERVICES builds. It exits non-zero on a missing image, and the "
        f"orchestrator waits for it, so `embry0 start` would never start the orchestrator."
    )
