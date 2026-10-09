"""job_title_from_task — the identifying line for webhook consumers."""

from __future__ import annotations

from embry0.workflows.issue_to_pr.plan_route import job_title_from_task


def test_takes_the_first_line_not_the_whole_body():
    task = "feat(pdftools): raw PDF tools wave 2\n\n## Why\n\nlong body text..."
    assert job_title_from_task(task) == "feat(pdftools): raw PDF tools wave 2"


def test_skips_leading_blank_lines():
    assert job_title_from_task("\n\n\nfix(api): stop double-charging\nbody") == "fix(api): stop double-charging"


def test_strips_markdown_heading_markers():
    assert job_title_from_task("# Fix the thing\n\nbody") == "Fix the thing"
    assert job_title_from_task("### Fix the thing") == "Fix the thing"


def test_truncates_rather_than_dumping():
    out = job_title_from_task("x" * 400)
    assert len(out) == 141  # 140 + ellipsis
    assert out.endswith("…")


def test_short_titles_are_not_given_an_ellipsis():
    assert not job_title_from_task("short title").endswith("…")


def test_empty_and_none_are_empty_string():
    assert job_title_from_task("") == ""
    assert job_title_from_task(None) == ""
    assert job_title_from_task("\n\n   \n") == ""


def test_a_heading_only_line_of_hashes_does_not_crash():
    assert job_title_from_task("###\n\nreal title") == "real title"


def test_a_leading_issue_ref_is_not_a_heading_marker():
    assert job_title_from_task("#123 fix the thing\n\nbody") == "#123 fix the thing"
