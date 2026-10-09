"""The production watcher is built with every repository it needs (#369).

`WatcherService.__init__` takes `signatures_repo` optionally, so omitting it is
not a TypeError — it is a watcher that clusters, logs a shadow comparison every
tick, and never writes a row. `log_signatures` was empty for the whole of
#289's shadow period because of exactly that.

This is asserted against the source rather than a live app because the wiring
lives in `lifespan`, which needs Docker and a database; `_init_app_state`, the
part the `api_client` fixture runs, does not build the watcher. The AST is
read rather than the text so reformatting cannot silently defeat the check.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import embry0.api.app as app_module

#: Constructor arguments the deployed watcher cannot do its job without. Each
#: one is optional in the signature, so a missing one fails silently at
#: runtime rather than loudly at startup — which is the reason for this test.
REQUIRED_IN_PRODUCTION = (
    "config",
    "jobs_repo",
    "watcher_runs_repo",
    "integration_repo",
    "executor",
    "patterns_repo",
    "cases_repo",
    "case_recorder",
    "deploy_sync",
    # #369 — without this, clustering measures nothing.
    "signatures_repo",
    # #378 — without this the tick records an error instead of filing.
    "filer",
)


def _watcher_service_call() -> ast.Call:
    source = pathlib.Path(app_module.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "WatcherService":
            return node
    pytest.fail("no WatcherService(...) construction found in embry0/api/app.py")


def test_production_wiring_passes_every_repository_the_watcher_needs() -> None:
    call = _watcher_service_call()
    passed = {kw.arg for kw in call.keywords if kw.arg}
    missing = [name for name in REQUIRED_IN_PRODUCTION if name not in passed]
    assert not missing, (
        f"embry0/api/app.py builds WatcherService without {missing}. "
        "Every one of these is an optional ctor argument, so the watcher will start "
        "and quietly do less than it reports."
    )


def test_signatures_repo_is_a_log_signatures_repository() -> None:
    """Pinned separately: passing the wrong repository here would satisfy the
    test above while still writing nothing to `log_signatures`."""
    call = _watcher_service_call()
    passed = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    node = passed.get("signatures_repo")
    assert node is not None, "signatures_repo is not passed at all"
    assert isinstance(node, ast.Attribute), f"expected app.state.<attr>, got {ast.dump(node)}"

    source = pathlib.Path(app_module.__file__).read_text()
    assigned = [
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "LogSignaturesRepository"
    ]
    assert assigned, "app.py never constructs a LogSignaturesRepository"
    targets = {ast.unparse(t) for a in assigned for t in a.targets}
    assert ast.unparse(node) in targets, (
        f"signatures_repo={ast.unparse(node)} is not the LogSignaturesRepository built in app.py "
        f"(that one is assigned to {sorted(targets)})"
    )


# ---- #312: the confirm-healed sweep ---------------------------------------


def _confirm_sweep_call() -> ast.Call:
    source = pathlib.Path(app_module.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ConfirmHealedSweep":
            return node
    pytest.fail("no ConfirmHealedSweep(...) construction found in embry0/api/app.py")


def test_confirm_healed_is_wired_with_the_signature_memory() -> None:
    """The sweep counts through `log_signatures`. Built without that repository
    it would read every signature as absent and call every case healed — the
    one wrong answer that closes a ticket."""
    passed = {kw.arg for kw in _confirm_sweep_call().keywords if kw.arg}
    for required in ("cases_repo", "cases_service", "signatures_repo", "config"):
        assert required in passed, f"ConfirmHealedSweep is built without {required!r}"


def test_the_filer_is_built_per_stack() -> None:
    """A watcher whose filer is None files nowhere and records an error every
    tick. Since #378 the filer follows the stack's own tracker, not the
    deployment's Linear config — build_filer decides (tested in
    tests/unit/services/test_watcher_multi_stack.py)."""
    call = _watcher_service_call()
    passed = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    assert ast.unparse(passed["filer"]).startswith("build_filer(stack")


def test_the_stack_table_is_wired_and_seeded_at_startup() -> None:
    """Without the seed an upgrading deployment starts with no stacks at all:
    the watcher would have nothing to watch until someone wrote a row."""
    source = pathlib.Path(app_module.__file__).read_text()
    tree = ast.parse(source)

    assigned = {
        ast.unparse(target)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if ast.unparse(node.value).startswith("WatcherStacksRepository(")
    }
    assert "app.state.watcher_stacks_repo" in assigned, "app.py never builds the stacks repository"

    seeded = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "seed_stacks"
    ]
    assert seeded, "app.py never seeds the env stack (services/watcher_stacks.seed_stacks)"
