"""The stored result's merge bit and the PR comment (#298, #299)."""

from __future__ import annotations

from embry0.workflows.verification.gate import GateReport, TargetRun, build_report, compare_target
from embry0.workflows.verification.render import COMMENT_MARKER, render_verification_comment
from embry0.workflows.verification.result import compose_result
from embry0.workflows.verification.tiers import classify_diff, plan_browser_qa

JAVA = "platform/api/src/main/java/Lead.java"
TSX = "frontend/apps/leads/src/Todo.tsx"
SECRET_PATH = "/workspace/platform/secret-output-line"


def _run(side: str, outcome: str, failed: list[str] | None = None) -> TargetRun:
    return TargetRun("jvm", side, 1, outcome, 0 if outcome == "passed" else 1, 5, failed, None, SECRET_PATH)  # type: ignore[arg-type]


def _gate(head: TargetRun, base: TargetRun | None) -> GateReport:
    return build_report(
        targets=[compare_target([head], base)],
        runs=[head, *([base] if base else [])],
        base_sha="b" * 40,
        head_sha="h" * 40,
        base_ref="main",
    )


def _result(files: list[str], gate: GateReport, *, managed: list[str] | None = None, needs_qa: bool = True) -> dict:
    tier = classify_diff(files)
    plan = plan_browser_qa(tier=tier.tier, needs_qa=needs_qa, managed_apps=managed or [], deployed_apps=["quoting"])
    return compose_result(mode="on", tier=tier, gate=gate, plan=plan)


def test_backend_only_with_a_green_gate_is_machine_mergeable() -> None:
    result = _result([JAVA], _gate(_run("head", "passed"), _run("base", "passed")))
    assert result["human_merge_only"] is False
    assert result["human_merge_reasons"] == []
    assert result["browser_qa"] == {"planned": "skipped", "reason_code": "backend_only"}
    assert result["blocking"] is False


def test_frontend_without_a_branch_stack_is_human_merge_only() -> None:
    result = _result([TSX], _gate(_run("head", "passed"), _run("base", "passed")))
    assert result["human_merge_only"] is True
    assert result["human_merge_reasons"] == ["no_branch_stack"]
    assert result["blocking"] is False


def test_code_that_no_gate_covered_is_human_merge_only_but_docs_are_not() -> None:
    unconfigured = GateReport.without_runs("not_configured")
    assert _result([JAVA], unconfigured)["human_merge_reasons"] == ["gate_not_configured"]
    assert _result(["docs/a.md"], unconfigured)["human_merge_only"] is False


def test_a_red_base_compared_by_exit_code_needs_a_person() -> None:
    result = _result([JAVA], _gate(_run("head", "failed"), _run("base", "failed")))
    assert result["blocking"] is False
    assert result["human_merge_reasons"] == ["gate_base_red_unverified"]
    assert "cannot be certain this change adds nothing" in render_verification_comment(result)


def test_a_preexisting_failure_does_not_block_but_does_need_a_person() -> None:
    """Not blocking (the change did not cause it), not machine-mergeable
    either: the base is red, so the comparison rests on a parser."""
    result = _result([JAVA], _gate(_run("head", "failed", ["T.flap"]), _run("base", "failed", ["T.flap"])))
    assert result["blocking"] is False
    assert result["human_merge_reasons"] == ["gate_base_red_unverified"]


def test_new_failures_block_and_are_named_on_the_pr() -> None:
    result = _result([JAVA], _gate(_run("head", "failed", ["LeadTest.idleTimer"]), _run("base", "passed")))
    assert result["blocking"] is True
    body = render_verification_comment(result)
    assert body.startswith(COMMENT_MARKER)
    assert "breaks tests that passed before it" in body
    assert "`LeadTest.idleTimer`" in body
    assert "What to do" in body


def test_the_comment_never_carries_command_output() -> None:
    for gate in (
        _gate(_run("head", "failed", ["T.a"]), _run("base", "passed")),
        _gate(_run("head", "failed"), _run("base", "failed")),
        _gate(_run("head", "errored"), None),
    ):
        assert SECRET_PATH not in render_verification_comment(_result([JAVA], gate))


def test_not_configured_says_so_and_does_not_block() -> None:
    body = render_verification_comment(_result([JAVA], GateReport.without_runs("not_configured")))
    assert "No test command is set up in embry0 for this repository" in body
    assert "does not block" in body


def test_frontend_without_branch_stack_tells_a_person_to_merge() -> None:
    body = render_verification_comment(_result([TSX], GateReport.without_runs("not_configured")))
    assert "testing the live site would test the code that is already released" in body
    assert "**Merging: by a person only.**" in body


def test_frontend_with_a_branch_stack_announces_the_browser_check() -> None:
    gate = _gate(_run("head", "passed"), _run("base", "passed"))
    body = render_verification_comment(_result([TSX], gate, managed=["web"]))
    assert "Browser check: runs next" in body
    assert "by a person only" not in body


def test_an_unknown_stored_verdict_still_renders() -> None:
    assert "could not be run" in render_verification_comment({"gate": {"verdict": "something-new"}})


# ── a red base is never silently trusted (#341 review) ──────────────────


def _red_base(head_names: list[str], base_names: list[str], granularity: str = "test") -> GateReport:
    def run(side: str, names: list[str]) -> TargetRun:
        return TargetRun("t", side, 1, "failed", 1, 5, names, None, "", granularity)  # type: ignore[arg-type]

    head, base = run("head", head_names), run("base", base_names)
    return build_report(
        targets=[compare_target([head], base)], runs=[head, base], base_sha="b", head_sha="h", base_ref="main"
    )


def test_a_file_level_comparison_that_found_nothing_new_still_needs_a_person() -> None:
    gate = _red_base(["src/a.test.tsx"], ["src/a.test.tsx"], granularity="file")
    result = _result([TSX], gate, managed=["web"])
    assert gate.targets[0].comparison == "files"
    assert result["blocking"] is False
    assert "gate_base_red_unverified" in result["human_merge_reasons"]
    assert "cannot be certain this change adds nothing" in render_verification_comment(result)


def test_even_a_test_level_pre_existing_failure_is_flagged() -> None:
    result = _result([JAVA], _red_base(["T.flap"], ["T.flap"]))
    assert result["human_merge_reasons"] == ["gate_base_red_unverified"]
    assert result["blocking"] is False


def test_a_new_failure_found_at_file_level_still_blocks() -> None:
    gate = _red_base(["src/a.test.tsx", "src/b.test.tsx"], ["src/a.test.tsx"], granularity="file")
    result = _result([TSX], gate, managed=["web"])
    assert result["blocking"] is True
    assert "gate_new_failures" in result["human_merge_reasons"]
