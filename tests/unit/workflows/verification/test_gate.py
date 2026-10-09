"""The base-vs-head comparison (#299) — pure, no sandbox."""

from __future__ import annotations

import json

from embry0.workflows.verification.gate import (
    GateReport,
    TargetRun,
    build_report,
    compare_target,
    parse_test_output,
)


def _run(side: str, outcome: str, failed: list[str] | None = None, attempt: int = 1) -> TargetRun:
    return TargetRun(
        target="jvm",
        side=side,
        attempt=attempt,
        outcome=outcome,
        exit_code={"passed": 0, "failed": 1, "timed_out": 124, "errored": None}[outcome],
        duration_ms=10,
        failed_tests=failed,
        summary=None,
        output_tail="",
    )


# ── parse_test_output ────────────────────────────────────────────────────


def test_pytest_short_summary_is_parsed() -> None:
    out = (
        "FAILED tests/unit/test_a.py::test_one - AssertionError: nope\n"
        "ERROR tests/unit/test_b.py::test_two\n"
        "==== 1 failed, 1 error, 40 passed in 3.21s ====\n"
    )
    parsed = parse_test_output(out)
    assert parsed.failed_tests == ["tests/unit/test_a.py::test_one", "tests/unit/test_b.py::test_two"]
    assert parsed.summary == "1 failed, 1 error, 40 passed in 3.21s"


def test_maven_surefire_summary_is_parsed() -> None:
    out = (
        "[ERROR] Failures: \n"
        "[ERROR]   LeadIdleTimerServiceImplTest.returnsToPool:42 expected: <1> but was: <2>\n"
        "[ERROR] Errors: \n"
        "[ERROR]   PortalAccountServiceWriteTest.createsStaff:17 » NullPointer\n"
        "[ERROR] Tests run: 212, Failures: 1, Errors: 1, Skipped: 0\n"
        "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.2.5:test\n"
    )
    parsed = parse_test_output(out)
    assert parsed.failed_tests == [
        "LeadIdleTimerServiceImplTest.returnsToPool",
        "PortalAccountServiceWriteTest.createsStaff",
    ]
    assert parsed.summary == "Tests run: 212, Failures: 1, Errors: 1, Skipped: 0"


def test_vitest_and_go_failures_are_parsed_through_ansi_colour() -> None:
    out = "\x1b[31m FAIL \x1b[0m src/__tests__/Todo.test.tsx > list > shows who\n--- FAIL: TestIdleTimer (0.01s)\n"
    assert parse_test_output(out).failed_tests == ["TestIdleTimer", "src/__tests__/Todo.test.tsx > list > shows who"]


def test_unrecognised_output_yields_no_names() -> None:
    parsed = parse_test_output("BUILD FAILURE: cannot find symbol")
    assert (parsed.failed_tests, parsed.summary, parsed.granularity) == (None, None, None)


# ── compare_target ───────────────────────────────────────────────────────


def test_head_green_passes_without_a_base_run() -> None:
    verdict = compare_target([_run("head", "passed")], None)
    assert verdict.status == "passed"
    assert verdict.blocking is False


def test_head_green_over_a_red_base_records_what_was_fixed() -> None:
    verdict = compare_target([_run("head", "passed")], _run("base", "failed", ["T.a"]))
    assert verdict.status == "passed"
    assert verdict.fixed == ["T.a"]


def test_red_head_over_green_base_is_a_new_failure() -> None:
    verdict = compare_target([_run("head", "failed", ["T.a"])], _run("base", "passed"))
    assert verdict.status == "new_failures"
    assert verdict.new_failures == ["T.a"]
    assert verdict.blocking is True


def test_same_test_red_on_both_sides_is_preexisting_not_blocking() -> None:
    verdict = compare_target([_run("head", "failed", ["T.flap"])], _run("base", "failed", ["T.flap"]))
    assert verdict.status == "preexisting_failures"
    assert verdict.preexisting == ["T.flap"]
    assert verdict.comparison == "tests"
    assert verdict.blocking is False


def test_a_new_failure_hiding_behind_a_red_base_still_blocks() -> None:
    verdict = compare_target([_run("head", "failed", ["T.flap", "T.new"])], _run("base", "failed", ["T.flap"]))
    assert verdict.status == "new_failures"
    assert verdict.new_failures == ["T.new"]
    assert verdict.preexisting == ["T.flap"]


def test_red_on_both_sides_without_test_names_falls_back_to_exit_codes() -> None:
    """Base cannot build: nothing can be compared, nothing is blamed on head."""
    verdict = compare_target([_run("head", "failed")], _run("base", "failed"))
    assert verdict.status == "preexisting_failures"
    assert verdict.comparison == "exit_code"
    assert verdict.blocking is False


def test_a_red_head_with_no_parsed_names_is_not_compared_by_name() -> None:
    """A head that stops compiling prints no FAILED lines — an empty name set
    minus the base's names must not read as 'nothing new'."""
    verdict = compare_target([_run("head", "failed", [])], _run("base", "failed", ["T.flap"]))
    assert verdict.comparison == "exit_code"


def test_a_timeout_counts_as_a_failure() -> None:
    verdict = compare_target([_run("head", "timed_out")], _run("base", "passed"))
    assert verdict.status == "new_failures"


def test_a_passing_rerun_makes_the_failure_flaky() -> None:
    verdict = compare_target(
        [_run("head", "failed", ["T.a"]), _run("head", "passed", attempt=2)],
        _run("base", "passed"),
    )
    assert verdict.status == "flaky"
    assert verdict.flaky == ["T.a"]
    assert verdict.blocking is False


def test_only_failures_common_to_every_head_attempt_are_new() -> None:
    verdict = compare_target(
        [_run("head", "failed", ["T.a", "T.b"]), _run("head", "failed", ["T.b"], attempt=2)],
        _run("base", "passed"),
    )
    assert verdict.status == "new_failures"
    assert verdict.new_failures == ["T.b"]
    assert verdict.flaky == ["T.a"]


def test_disjoint_failures_across_head_attempts_are_flaky() -> None:
    verdict = compare_target(
        [_run("head", "failed", ["T.a"]), _run("head", "failed", ["T.b"], attempt=2)],
        _run("base", "passed"),
    )
    assert verdict.status == "flaky"


def test_an_unrunnable_head_is_an_error_not_a_pass() -> None:
    verdict = compare_target([_run("head", "errored")], None)
    assert verdict.status == "error"
    assert verdict.blocking is False


def test_a_red_head_with_no_usable_base_is_an_error() -> None:
    assert compare_target([_run("head", "failed")], None).status == "error"
    assert compare_target([_run("head", "failed")], _run("base", "errored")).status == "error"


# ── build_report ─────────────────────────────────────────────────────────


def _report(*verdict_runs: tuple[list[TargetRun], TargetRun | None]) -> GateReport:
    targets = [compare_target(h, b) for h, b in verdict_runs]
    runs = [r for h, b in verdict_runs for r in [*h, *([b] if b else [])]]
    return build_report(targets=targets, runs=runs, base_sha="b" * 40, head_sha="h" * 40, base_ref="main")


def test_report_verdict_precedence() -> None:
    green = ([_run("head", "passed")], _run("base", "passed"))
    new = ([_run("head", "failed")], _run("base", "passed"))
    old = ([_run("head", "failed")], _run("base", "failed"))
    err = ([_run("head", "errored")], None)
    assert _report(green).verdict == "passed"
    assert _report(green, old).verdict == "unstable"
    assert _report(green, old, err).verdict == "error"
    assert _report(green, old, err, new).verdict == "new_failures"
    assert _report(green, old, err, new).blocking is True
    assert _report(green, old, err).blocking is False


def test_report_round_trips_through_json() -> None:
    report = _report(([_run("head", "failed", ["T.a"])], _run("base", "passed")))
    data = json.loads(json.dumps(report.to_dict()))
    assert data["schema_version"] == 1
    assert data["verdict"] == "new_failures"
    assert data["blocking"] is True
    assert data["targets"][0]["new_failures"] == ["T.a"]
    assert {r["side"] for r in data["runs"]} == {"base", "head"}
    assert GateReport.from_dict(data).to_dict() == data


def test_unconfigured_and_unmatched_reports_never_block() -> None:
    for verdict in ("not_configured", "no_matching_targets"):
        report = GateReport.without_runs(verdict, note="why")
        assert report.blocking is False
        assert report.to_dict()["targets"] == []


# ── granularity: what a parsed name set can actually prove ───────────────
#
# Review of #341: `FAIL <file>` captures a FILE, so a genuine regression
# inside a file that already had a failure compared as "nothing new" and the
# job was not even flagged. A name set is only conclusive at TEST
# granularity; anything coarser has to say so.


def test_vitest_names_a_test_not_a_file() -> None:
    parsed = parse_test_output("\x1b[31m FAIL \x1b[0m src/__tests__/Todo.test.tsx > list > shows who\n")
    assert parsed.failed_tests == ["src/__tests__/Todo.test.tsx > list > shows who"]
    assert parsed.granularity == "test"


def test_a_bare_fail_line_is_only_file_granular() -> None:
    parsed = parse_test_output(" FAIL  src/__tests__/Todo.test.tsx\n")
    assert parsed.failed_tests == ["src/__tests__/Todo.test.tsx"]
    assert parsed.granularity == "file"


def test_one_file_granular_line_makes_the_whole_set_file_granular() -> None:
    parsed = parse_test_output(" FAIL src/a.test.tsx > s > a\n FAIL src/b.test.tsx\n")
    assert parsed.granularity == "file"


def test_a_pytest_id_keeps_its_spaces() -> None:
    parsed = parse_test_output(
        "FAILED tests/test_a.py::test_p[case one] - AssertionError: nope\n"
        "FAILED tests/test_a.py::test_p[case two] - AssertionError: nope\n"
    )
    assert parsed.failed_tests == ["tests/test_a.py::test_p[case one]", "tests/test_a.py::test_p[case two]"]
    assert parsed.granularity == "test"


def test_pytest_and_maven_and_go_stay_test_granular() -> None:
    for text in (
        "FAILED tests/x.py::test_a - boom\n",
        "[ERROR]   FooTest.bar:42 expected\n",
        "--- FAIL: TestIdleTimer (0.01s)\n",
    ):
        assert parse_test_output(text).granularity == "test"


def _file_run(side: str, failed: list[str] | None) -> TargetRun:
    return TargetRun(
        target="web",
        side=side,
        attempt=1,
        outcome="failed" if failed is not None else "passed",
        exit_code=1 if failed is not None else 0,
        duration_ms=10,
        failed_tests=failed,
        summary=None,
        output_tail="",
        name_granularity="file",
    )


def test_a_new_failure_inside_an_already_red_file_is_not_called_preexisting() -> None:
    """The gate may not claim 'nothing new' from a file-level comparison."""
    verdict = compare_target([_file_run("head", ["src/a.test.tsx"])], _file_run("base", ["src/a.test.tsx"]))
    assert verdict.status == "preexisting_failures"
    assert verdict.comparison == "files"
    assert verdict.blocking is False


def test_a_newly_red_file_still_blocks() -> None:
    verdict = compare_target(
        [_file_run("head", ["src/a.test.tsx", "src/b.test.tsx"])], _file_run("base", ["src/a.test.tsx"])
    )
    assert verdict.status == "new_failures"
    assert verdict.new_failures == ["src/b.test.tsx"]
    assert verdict.comparison == "files"


def test_a_red_base_against_a_green_head_is_unaffected_by_granularity() -> None:
    assert compare_target([_file_run("head", None)], _file_run("base", ["src/a.test.tsx"])).status == "passed"
