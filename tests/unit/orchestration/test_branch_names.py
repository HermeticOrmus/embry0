"""job_branch_name — one derivation for the developer node and merge sync (#279)."""

import pytest

from embry0.orchestration.branch_names import job_branch_name, job_branch_names, shared_branch_name

JOB = "job-5f12020ec6a7"


@pytest.mark.parametrize(
    ("issue_id", "task", "context", "job_id", "expected"),
    [
        (
            "iss-2c5413502fa5",
            "[ENG-1109] [watcher] Fix lazy init",
            None,
            JOB,
            "embry0/iss-2c541350-eng-1109-watcher-fix-lazy",
        ),
        (
            None,
            "Backend: idle-timer auto-return for tasks",
            None,
            JOB,
            "embry0/unknown-backend-idle-timer-auto-retur-5f12020e",
        ),
        (
            "",
            "Add billing terms (prepaid / monthly)",
            {"type": "git"},
            JOB,
            "embry0/unknown-add-billing-terms-prepaid-m-5f12020e",
        ),
        (None, "???", None, JOB, "embry0/unknown-task-5f12020e"),
        (None, None, None, JOB, "embry0/unknown-task-5f12020e"),
        # No job id to make it the job's own: the shared name, as before #558.
        (None, "Backend: idle-timer auto-return for tasks", None, None, "embry0/unknown-backend-idle-timer-auto-retur"),
        ("iss-abc", "anything", {"ref": "embry0/existing"}, JOB, "embry0/existing"),
        (None, "anything", {"ref": "embry0/existing"}, JOB, "embry0/existing"),
    ],
)
def test_job_branch_name(issue_id, task, context, job_id, expected):
    assert job_branch_name(issue_id=issue_id, task=task, job_id=job_id, context=context) == expected


def test_the_developer_node_uses_the_same_derivation():
    from embry0.workflows.issue_to_pr.nodes import _developer_branch_name

    for issue_id in ("iss-2c5413502fa5", None):
        state = {"issue_id": issue_id, "task": "[ENG-1109] [watcher] Fix lazy init", "job_id": JOB}
        assert _developer_branch_name(state) == job_branch_name(
            issue_id=state["issue_id"], task=state["task"], job_id=JOB
        ), f"the developer node and branch_names disagree for issue_id={issue_id!r}"


def test_issueless_jobs_whose_tasks_begin_the_same_way_derive_different_branches():
    """#558: two such jobs dispatched together both found the shared name free and pushed over each other."""
    task = "Deploy proof job 2026-10-08: append one proof line"
    shared = shared_branch_name(issue_id=None, task=task)

    first = job_branch_name(issue_id=None, task=task, job_id="job-bc25a9c3ded1")
    second = job_branch_name(issue_id=None, task=task + " (second)", job_id="job-2af313479fa5")

    assert first != second, f"issue-less jobs with one task prefix must not share a branch: {first!r}"
    assert (first, second) == (f"{shared}-bc25a9c3", f"{shared}-2af31347")


def test_every_job_of_an_issue_still_derives_the_issue_name():
    """The issue name is shared on purpose: #478 probes it, and the open-PR duplicate check reads its prefix."""
    jobs = (JOB, "job-0a0b0c0d0e0f")
    names = {job_branch_name(issue_id="iss-81a36c858147", task="Fix vendor", job_id=job) for job in jobs}

    assert names == {"embry0/iss-81a36c85-fix-vendor"}, names


def test_a_taken_name_gets_the_job_id_and_keeps_the_issue_prefix():
    """#478. The open-PR duplicate check reads the issue prefix, so a job's own branch must keep it."""
    from embry0.orchestration.branch_names import taken_branch_name
    from embry0.services.github_prs import parse_embry0_branch_issue_prefix

    derived = job_branch_name(issue_id="iss-81a36c858147", task="[ENG-2435] [watcher] Fix vendor", job_id=JOB)
    own = taken_branch_name(derived, JOB)

    assert own == f"{derived}-5f12020e"
    assert parse_embry0_branch_issue_prefix(own) == "iss-81a36c85", "duplicate detection must still see the issue"


def test_a_job_may_have_pushed_its_derived_branch_or_its_own_and_a_follow_up_only_its_ref():
    derived = job_branch_name(issue_id="iss-81a36c858147", task="Fix vendor", job_id=JOB)
    assert job_branch_names(issue_id="iss-81a36c858147", task="Fix vendor", job_id=JOB) == {
        derived,
        f"{derived}-5f12020e",
    }
    assert job_branch_names(
        issue_id="iss-81a36c858147", task="Fix vendor", context={"ref": "feature/x"}, job_id=JOB
    ) == {"feature/x"}


@pytest.mark.parametrize("issue_id", ["iss-81a36c858147", None])
def test_merge_sync_matches_the_branch_the_developer_derives(issue_id):
    """A reader that re-derived without the job id would stop finding an issue-less job's branch (#558)."""
    from embry0.services.merge_sync.recorder import branches_of
    from embry0.workflows.issue_to_pr.nodes import _developer_branch_name

    row = {"job_id": JOB, "issue_id": issue_id, "task": "Fix vendor", "context": None}
    pushed = _developer_branch_name(dict(row))

    assert pushed in branches_of(row), (
        f"merge sync must match the branch the developer pushes: {pushed!r} not in {sorted(branches_of(row))}"
    )


def test_merge_sync_still_matches_the_shared_name_of_an_issueless_job_from_before_558():
    """Those rows look the same as new ones, and their developer pushed the shared name."""
    from embry0.services.merge_sync.recorder import branches_of

    row = {"job_id": JOB, "issue_id": None, "task": "Fix vendor", "context": None}

    assert "embry0/unknown-fix-vendor" in branches_of(row), (
        f"a job created before #558 pushed the shared name; merge sync must still find it: {sorted(branches_of(row))}"
    )
