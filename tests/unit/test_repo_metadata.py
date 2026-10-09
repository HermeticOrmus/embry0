"""#532: packaging and tooling facts that two files state, kept in agreement.

Each fact below is written in more than one place, and nothing else notices
when the copies drift apart:

- pyproject's `readme` must exist. The orchestrator image installs the package
  from a stub tree, so its Dockerfile must provide that file too, or hatchling
  fails the image build with "Readme file does not exist".
- `.python-version` (the interpreter uv gives contributors) must be the Python
  the images run and the floor `requires-python` declares.
- the pre-commit ruff hook must be the ruff `uv.lock` pins, or the hook and
  `uv run ruff` disagree about the same file.
- the version is written in pyproject, the frontend's package files and the
  API's OpenAPI document; a release bumps them together and gives the version a
  CHANGELOG entry (docs/releasing.md).
"""

import ast
import fnmatch
import json
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_DOCKERFILE = ROOT / "infra" / "Dockerfile.orchestrator"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _instructions(dockerfile: Path) -> list[str]:
    """Dockerfile instructions, one per entry: continuations joined, comments dropped."""
    lines = [ln for ln in dockerfile.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]
    return [ins.strip() for ins in "\n".join(lines).replace("\\\n", " ").splitlines() if ins.strip()]


def _dockerignored(name: str) -> bool:
    """Whether .dockerignore keeps `name` (a root-level file) out of the build context."""
    ignored = False
    for raw in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negate = pattern.startswith("!")
        if fnmatch.fnmatch(name, pattern.lstrip("!").lstrip("/")):
            ignored = not negate
    return ignored


def _readme() -> str:
    readme = _pyproject()["project"].get("readme")
    assert isinstance(readme, str), f"pyproject.toml declares no readme file (project.readme = {readme!r})"
    return readme


def test_pyproject_readme_exists():
    readme = _readme()
    assert (ROOT / readme).is_file(), f"pyproject.toml names readme {readme!r}, which is not a file in the repo"


def test_orchestrator_image_provides_the_readme_before_installing():
    readme = _readme()
    instructions = _instructions(ORCHESTRATOR_DOCKERFILE)
    install_at = next(
        (
            i
            for i, ins in enumerate(instructions)
            if ins.startswith("RUN") and re.search(r"pip install\b.*\s-e\s+\.", ins)
        ),
        None,
    )
    assert install_at is not None, "Dockerfile.orchestrator no longer runs `pip install -e .`; update this guard"
    upto_install = instructions[: install_at + 1]
    touched = any(ins.startswith("RUN") and re.search(rf"\btouch\s+{re.escape(readme)}\b", ins) for ins in upto_install)
    copied = any(ins.startswith("COPY") and readme in ins.split()[1:-1] for ins in upto_install)
    assert touched or (copied and not _dockerignored(readme)), (
        f"Dockerfile.orchestrator runs `pip install -e .` without {readme} in place: hatchling reads pyproject's "
        f"`readme` and fails the image build. Create it before the install"
        + (f" ({readme} is COPYed, but .dockerignore drops it from the build context)" if copied else "")
    )


def test_python_version_file_matches_requires_python_and_the_images():
    path = ROOT / ".python-version"
    assert path.is_file(), ".python-version is missing: uv then picks whatever Python it finds for contributors"
    pinned = path.read_text(encoding="utf-8").strip()

    requires = _pyproject()["project"]["requires-python"]
    floor = re.fullmatch(r">=\s*(\d+\.\d+)", requires)
    assert floor, f"requires-python {requires!r} is not a plain '>=X.Y' floor; update this guard"
    assert pinned == floor.group(1), f".python-version is {pinned}, requires-python floor is {floor.group(1)}"

    assert _pyproject()["tool"]["mypy"]["python_version"] == pinned, (
        "mypy checks a different Python than .python-version"
    )

    images = {
        f"{dockerfile.name}: {m.group(0)}": m.group(1)
        for dockerfile in sorted((ROOT / "infra").glob("Dockerfile*"))
        for m in re.finditer(r"^FROM python:(\d+\.\d+)\.\d+", dockerfile.read_text(encoding="utf-8"), re.MULTILINE)
    }
    assert images, "no infra/Dockerfile* starts FROM python:X.Y.Z; update this guard"
    mismatched = {image: minor for image, minor in images.items() if minor != pinned}
    assert not mismatched, f".python-version is {pinned}, but these images run another Python: {mismatched}"


def test_pre_commit_ruff_is_the_locked_ruff():
    path = ROOT / ".pre-commit-config.yaml"
    assert path.is_file(), ".pre-commit-config.yaml is missing"
    repos = yaml.safe_load(path.read_text(encoding="utf-8"))["repos"]
    ruff_repo = next((r for r in repos if r["repo"].rstrip("/").endswith("astral-sh/ruff-pre-commit")), None)
    assert ruff_repo is not None, ".pre-commit-config.yaml has no astral-sh/ruff-pre-commit hooks"
    hook_ids = {h["id"] for h in ruff_repo["hooks"]}
    assert hook_ids >= {"ruff-check", "ruff-format"}, (
        f"pre-commit must run ruff-check and ruff-format; it runs {hook_ids}"
    )

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = [p["version"] for p in lock["package"] if p["name"] == "ruff"]
    assert len(locked) == 1, f"uv.lock pins ruff {locked}"
    assert ruff_repo["rev"] == f"v{locked[0]}", (
        f"pre-commit runs ruff {ruff_repo['rev']}, uv.lock pins {locked[0]}: bump the hook's rev with the lock"
    )


def _openapi_version() -> str:
    tree = ast.parse((ROOT / "embry0" / "api" / "app.py").read_text(encoding="utf-8"))
    versions = [
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "FastAPI"
        for kw in node.keywords
        if kw.arg == "version" and isinstance(kw.value, ast.Constant)
    ]
    assert len(versions) == 1, f"expected one FastAPI(version=...) literal in embry0/api/app.py, found {versions}"
    return versions[0]


def test_version_agrees_everywhere_and_has_a_changelog_entry():
    version = _pyproject()["project"]["version"]
    package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    copies = {
        "frontend/package.json": package["version"],
        "frontend/package-lock.json": lock["version"],
        "frontend/package-lock.json packages['']": lock["packages"][""]["version"],
        "embry0/api/app.py FastAPI(version=)": _openapi_version(),
    }
    stale = {where: v for where, v in copies.items() if v != version}
    assert not stale, f"pyproject.toml is at {version}, these are not: {stale}"

    changelog = ROOT / "CHANGELOG.md"
    assert changelog.is_file(), "CHANGELOG.md is missing"
    headings = re.findall(r"^## \[([^\]]+)\]", changelog.read_text(encoding="utf-8"), re.MULTILINE)
    assert version in headings, f"CHANGELOG.md has no '## [{version}]' entry (its entries: {headings})"
