"""format_review_scope — size-bounded changed-files prompt block (RAV-1405).

The scope block rides the sandbox runner's --config JSON as one exec argv
element; an unbounded list (3994 files against a wrong diff base) exceeded
ARG_MAX and killed the job. The block must stay bounded for any input size.
"""

from __future__ import annotations

from embry0.workflows.issue_to_pr.nodes._review import SCOPE_FILES_CAP, format_review_scope


def test_empty_diff_gives_unscoped_prompt() -> None:
    scope_block, diff_step = format_review_scope([], "master")
    assert scope_block == ""
    assert diff_step == "4. Review the git diff: `git diff master...HEAD`"


def test_small_diff_lists_every_file() -> None:
    scope_block, diff_step = format_review_scope(["a.py", "b/c.ts"], "master")
    assert "Changed files in this branch (2):" in scope_block
    assert "- a.py" in scope_block and "- b/c.ts" in scope_block
    assert "truncated" not in scope_block
    assert "git diff master...HEAD -- <path>" in diff_step


def test_huge_diff_is_clamped() -> None:
    files = [f"apps/x/file{i}.ts" for i in range(4000)]
    scope_block, _ = format_review_scope(files, "master")
    assert f"- {files[SCOPE_FILES_CAP - 1]}" in scope_block
    assert f"- {files[SCOPE_FILES_CAP]}" not in scope_block
    assert f"and {4000 - SCOPE_FILES_CAP} more" in scope_block
    assert "Changed files in this branch (4000):" in scope_block
    # The whole block stays far below ARG_MAX territory regardless of input.
    assert len(scope_block) < 20_000


def test_clamp_boundary_exact_cap_is_not_truncated() -> None:
    files = [f"f{i}.py" for i in range(SCOPE_FILES_CAP)]
    scope_block, _ = format_review_scope(files, "main")
    assert "truncated" not in scope_block
