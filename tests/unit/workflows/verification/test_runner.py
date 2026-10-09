"""The sandbox side of the gate (#299) — driven through a fake docker client."""

from __future__ import annotations

from typing import Any

import pytest

from embry0.workflows.verification.config import VerificationConfig
from embry0.workflows.verification.runner import BASE_DIR, HEAD_DIR, run_gate, select_targets

HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40


class FakeDocker:
    """Answers the prepare step, then each test run from ``results``.

    ``results`` maps (dir, command) to a list of (exit_code, output) consumed
    one per attempt; the last entry repeats.
    """

    def __init__(self, results: dict[tuple[str, str], list[tuple[int, str]]], *, prepare_fails: bool = False) -> None:
        self.results = {k: list(v) for k, v in results.items()}
        self.prepare_fails = prepare_fails
        self.runs: list[tuple[str, str]] = []
        self.timeouts: list[int] = []
        self.cleaned = False

    def build_exec_cmd(self, container: str, command: list[str], workdir: Any = None, env: Any = None, **_: Any):
        return {"container": container, "command": command, "env": env or {}}

    async def run_cmd(self, cmd: dict[str, Any], timeout: int = 60) -> str:
        env = cmd["env"]
        script = cmd["command"][-1]
        if "EMBRY0_VERIFY_CMD" in env:
            key = (env["EMBRY0_VERIFY_DIR"], env["EMBRY0_VERIFY_CMD"])
            self.runs.append(key)
            self.timeouts.append(int(env["EMBRY0_VERIFY_TIMEOUT"]))
            queue = self.results[key]
            code, output = queue.pop(0) if len(queue) > 1 else queue[0]
            if code == -2:
                raise TimeoutError("docker command timed out")
            if code == -1:
                raise RuntimeError("Docker command failed: no such container")
            return f"{output}\nEMBRY0_VERIFY_RESULT exit={code} ms=1234"
        if "worktree add" in script:
            if self.prepare_fails:
                raise RuntimeError("Docker command failed: fatal: not a git repository")
            return f"EMBRY0_VERIFY_HEAD={HEAD_SHA}\nEMBRY0_VERIFY_BASE={BASE_SHA}"
        if "worktree remove" in script:
            self.cleaned = True
            return ""
        raise AssertionError(f"unexpected exec: {script[:80]}")


def _config(**overrides: Any) -> VerificationConfig:
    raw: dict[str, Any] = {
        "test_targets": [
            {"name": "jvm", "paths": ["platform/**"], "command": "cd platform && ./mvnw -q test"},
            {"name": "web", "paths": ["frontend/**"], "command": "cd frontend && npm test"},
        ],
        **overrides,
    }
    return VerificationConfig.model_validate(raw)


JVM = "cd platform && ./mvnw -q test"


async def _gate(docker: FakeDocker, config: VerificationConfig, changed: list[str]):
    return await run_gate(docker=docker, container_id="c1", config=config, changed_files=changed, base_ref="main")


def test_targets_are_selected_by_changed_paths() -> None:
    targets = _config().test_targets
    assert [t.name for t in select_targets(targets, ["platform/api/Foo.java"])] == ["jvm"]
    assert [t.name for t in select_targets(targets, ["docs/x.md"])] == []


def test_an_unknown_diff_selects_every_target() -> None:
    assert [t.name for t in select_targets(_config().test_targets, [])] == ["jvm", "web"]


def test_a_target_without_paths_always_runs() -> None:
    config = VerificationConfig.model_validate({"test_targets": [{"name": "all", "command": "make test"}]})
    assert [t.name for t in select_targets(config.test_targets, ["docs/x.md"])] == ["all"]


@pytest.mark.asyncio
async def test_green_head_and_base_pass_and_both_runs_are_kept() -> None:
    docker = FakeDocker({(HEAD_DIR, JVM): [(0, "ok")], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(), ["platform/api/Foo.java"])
    assert report.verdict == "passed"
    assert (report.base_sha, report.head_sha, report.base_ref) == (BASE_SHA, HEAD_SHA, "main")
    assert [(r.side, r.outcome, r.duration_ms) for r in report.runs] == [
        ("head", "passed", 1234),
        ("base", "passed", 1234),
    ]
    assert docker.cleaned is True


@pytest.mark.asyncio
async def test_base_run_can_be_skipped_when_head_is_green() -> None:
    docker = FakeDocker({(HEAD_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(base_run="on_head_failure"), ["platform/api/Foo.java"])
    assert report.verdict == "passed"
    assert docker.runs == [(HEAD_DIR, JVM)]


@pytest.mark.asyncio
async def test_a_new_failure_is_rerun_then_blocks() -> None:
    red = (1, "[ERROR]   FooTest.bar:12 expected 1\n[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0")
    docker = FakeDocker({(HEAD_DIR, JVM): [red], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(), ["platform/api/Foo.java"])
    assert report.verdict == "new_failures"
    assert report.targets[0].new_failures == ["FooTest.bar"]
    assert docker.runs == [(HEAD_DIR, JVM), (BASE_DIR, JVM), (HEAD_DIR, JVM)]
    assert [r.attempt for r in report.runs_for("head")] == [1, 2]


@pytest.mark.asyncio
async def test_a_flapping_baseline_is_not_rerun_and_does_not_block() -> None:
    red = (1, "FAILED tests/test_flap.py::test_flap - boom")
    docker = FakeDocker({(HEAD_DIR, JVM): [red], (BASE_DIR, JVM): [red]})
    report = await _gate(docker, _config(), ["platform/api/Foo.java"])
    assert report.verdict == "unstable"
    assert report.targets[0].preexisting == ["tests/test_flap.py::test_flap"]
    assert docker.runs == [(HEAD_DIR, JVM), (BASE_DIR, JVM)]


@pytest.mark.asyncio
async def test_a_passing_rerun_records_a_flake() -> None:
    docker = FakeDocker({(HEAD_DIR, JVM): [(1, "boom"), (0, "ok")], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(), ["platform/api/Foo.java"])
    assert report.verdict == "unstable"
    assert report.targets[0].status == "flaky"


@pytest.mark.asyncio
async def test_timeout_exit_code_is_a_timed_out_run() -> None:
    docker = FakeDocker({(HEAD_DIR, JVM): [(124, "")], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(head_reruns=0), ["platform/api/Foo.java"])
    assert report.runs_for("head")[0].outcome == "timed_out"
    assert report.verdict == "new_failures"
    assert docker.timeouts[0] == 1200


@pytest.mark.asyncio
async def test_a_wedged_exec_is_a_timed_out_run() -> None:
    docker = FakeDocker({(HEAD_DIR, JVM): [(-2, "")], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(head_reruns=0), ["platform/api/Foo.java"])
    assert report.runs_for("head")[0].outcome == "timed_out"
    assert report.verdict == "new_failures"


@pytest.mark.asyncio
async def test_a_docker_failure_is_an_error_verdict_not_a_pass() -> None:
    docker = FakeDocker({(HEAD_DIR, JVM): [(-1, "")], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(), ["platform/api/Foo.java"])
    assert report.verdict == "error"
    assert report.blocking is False


@pytest.mark.asyncio
async def test_a_workspace_that_cannot_be_prepared_is_an_error_verdict() -> None:
    report = await _gate(FakeDocker({}, prepare_fails=True), _config(), ["platform/api/Foo.java"])
    assert report.verdict == "error"
    assert report.runs == []


@pytest.mark.asyncio
async def test_no_matching_target_runs_nothing() -> None:
    docker = FakeDocker({})
    report = await _gate(docker, _config(), ["docs/x.md"])
    assert report.verdict == "no_matching_targets"
    assert docker.runs == []


@pytest.mark.asyncio
async def test_long_output_keeps_the_tail_but_parses_everything() -> None:
    noise = "\n".join(f"line {i}" for i in range(5000))
    red = (1, f"FAILED tests/a.py::test_x - boom\n{noise}")
    docker = FakeDocker({(HEAD_DIR, JVM): [red], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(head_reruns=0), ["platform/api/Foo.java"])
    run = report.runs_for("head")[0]
    assert run.failed_tests == ["tests/a.py::test_x"]
    assert len(run.output_tail) <= 8000
    assert run.output_tail.endswith("line 4999")


@pytest.mark.asyncio
async def test_an_exhausted_time_budget_stops_running_and_never_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    import embry0.workflows.verification.runner as runner

    clock = iter([0.0])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock, 99999.0))
    docker = FakeDocker({(HEAD_DIR, JVM): [(0, "ok")], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(), ["platform/api/Foo.java"])
    assert report.verdict == "error"
    assert "time budget" in report.note


# ── review of #341: what reaches the database, and what the names prove ──


@pytest.mark.asyncio
async def test_secrets_in_command_output_never_reach_the_stored_tail() -> None:
    leak = "XAI_API_KEY=xai-abcdef0123456789abcdef0123456789\nGITHUB_TOKEN=ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    docker = FakeDocker({(HEAD_DIR, JVM): [(1, leak)], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(head_reruns=0), ["platform/api/Foo.java"])
    tail = report.runs_for("head")[0].output_tail
    assert "xai-abcdef" not in tail and "ghp_0123" not in tail
    assert "***REDACTED***" in tail
    assert "XAI_API_KEY=" in tail  # the shape of the log survives


@pytest.mark.asyncio
async def test_a_file_level_runner_is_recorded_as_file_granular() -> None:
    docker = FakeDocker({(HEAD_DIR, JVM): [(1, " FAIL  src/a.test.tsx")], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(head_reruns=0), ["platform/api/Foo.java"])
    assert report.runs_for("head")[0].name_granularity == "file"


@pytest.mark.asyncio
async def test_a_test_level_runner_is_recorded_as_test_granular() -> None:
    out = " FAIL  src/a.test.tsx > cart > shows who"
    docker = FakeDocker({(HEAD_DIR, JVM): [(1, out)], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(head_reruns=0), ["platform/api/Foo.java"])
    run = report.runs_for("head")[0]
    assert run.name_granularity == "test"
    assert run.failed_tests == ["src/a.test.tsx > cart > shows who"]


@pytest.mark.asyncio
async def test_a_docker_client_that_does_not_strip_its_output_still_works() -> None:
    """The marker is the last line; depending on run_cmd's .strip() was a
    silent dependency that made every target 'errored' for a caller that
    did not strip (review of #341)."""

    class UnstrippedDocker(FakeDocker):
        async def run_cmd(self, cmd, timeout: int = 60) -> str:
            return await super().run_cmd(cmd, timeout) + "\n\n"

    docker = UnstrippedDocker({(HEAD_DIR, JVM): [(0, "ok")], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(), ["platform/api/Foo.java"])
    assert report.verdict == "passed"
    assert [r.outcome for r in report.runs] == ["passed", "passed"]


@pytest.fixture
def registered_secret():
    from embry0.safety.redaction import known_secret_values, register_known_secrets

    before = known_secret_values()
    register_known_secrets(["s3cr3t-nonstandard-value"])
    yield "s3cr3t-nonstandard-value"
    register_known_secrets(before, reset=True)


@pytest.mark.asyncio
async def test_a_registry_secret_never_reaches_the_stored_tail(registered_secret) -> None:
    """Spec 2026-09-29 §5: the registry plaintexts registered at startup are scrubbed by value,
    whatever their shape — the pattern list alone would pass this one through."""
    docker = FakeDocker(
        {(HEAD_DIR, JVM): [(1, f"clone with {registered_secret} failed")], (BASE_DIR, JVM): [(0, "ok")]}
    )
    report = await _gate(docker, _config(head_reruns=0), ["platform/api/Foo.java"])
    tail = report.runs_for("head")[0].output_tail
    assert registered_secret not in tail, tail
    assert "***REDACTED***" in tail


@pytest.mark.asyncio
async def test_a_secret_cut_by_the_tail_boundary_leaves_no_fragment(registered_secret) -> None:
    from embry0.workflows.verification.runner import STORED_TAIL_CHARS

    # The secret straddles the cut: only its last characters would fall inside the tail.
    body = registered_secret + "x" * (STORED_TAIL_CHARS - 10)
    docker = FakeDocker({(HEAD_DIR, JVM): [(1, body)], (BASE_DIR, JVM): [(0, "ok")]})
    report = await _gate(docker, _config(head_reruns=0), ["platform/api/Foo.java"])
    tail = report.runs_for("head")[0].output_tail
    assert registered_secret[-8:] not in tail, tail[:40]
