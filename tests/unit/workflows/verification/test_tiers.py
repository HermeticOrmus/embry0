"""The diff tier classifier and the browser-QA plan (#298) — pure functions."""

from __future__ import annotations

import pytest

from embry0.workflows.verification.config import TierRules
from embry0.workflows.verification.tiers import classify_diff, plan_browser_qa

JAVA = "platform/api/src/main/java/com/acme/LeadService.java"
JAVA_TEST = "platform/api/src/test/java/com/acme/LeadServiceTest.java"
TSX = "frontend/apps/leads/src/components/TodoSideCart.tsx"


def test_backend_files_only_is_backend_only() -> None:
    decision = classify_diff([JAVA, JAVA_TEST, "platform/api/src/main/resources/application.properties"])
    assert decision.tier == "backend_only"
    assert decision.unknown == []


def test_any_frontend_file_makes_the_change_frontend() -> None:
    decision = classify_diff([JAVA, TSX])
    assert decision.tier == "frontend"
    assert decision.frontend == [TSX]


def test_docs_only_is_neutral() -> None:
    assert classify_diff(["docs/adr/0002-portals.md", "README.md"]).tier == "neutral_only"


def test_docs_next_to_backend_stay_backend_only() -> None:
    assert classify_diff([JAVA, "docs/adr/0002-portals.md"]).tier == "backend_only"


@pytest.mark.parametrize("path", ["infra/docker-compose.yml", "package.json", "scripts/deploy.sh", "src/server.ts"])
def test_an_unknown_path_classifies_as_the_stricter_tier(path: str) -> None:
    decision = classify_diff([JAVA, path])
    assert decision.tier == "frontend"
    assert decision.unknown == [path]


def test_an_unknown_diff_is_the_stricter_tier() -> None:
    decision = classify_diff([])
    assert decision.tier == "frontend"
    assert "unknown" in decision.reason


def test_frontend_wins_over_backend_and_neutral_for_one_path() -> None:
    assert classify_diff(["frontend/scripts/gen.py"]).tier == "frontend"
    assert classify_diff(["frontend/README.md"]).tier == "frontend"


def test_per_repo_rules_replace_the_defaults_list_by_list() -> None:
    rules = TierRules(backend_paths=["platform/**", "ml/**", "infra/**"])
    assert classify_diff([JAVA, "infra/docker-compose.yml"], rules).tier == "backend_only"
    # frontend_paths was not overridden: its default still applies.
    assert classify_diff([JAVA, TSX], rules).tier == "frontend"
    # …and the backend default no longer does.
    assert classify_diff(["tools/x.py"], rules).tier == "frontend"


def test_an_explicit_empty_list_means_no_such_paths() -> None:
    assert classify_diff(["docs/x.md"], TierRules(neutral_paths=[])).tier == "frontend"


def test_windows_separators_and_dot_prefixes_are_normalised() -> None:
    assert classify_diff(["./platform\\api\\Foo.java"]).tier == "backend_only"


def test_decision_serialises_with_capped_lists() -> None:
    decision = classify_diff([f"frontend/f{i}.tsx" for i in range(80)])
    data = decision.to_dict()
    assert data["tier"] == "frontend"
    assert data["counts"]["frontend"] == 80
    assert len(data["frontend"]) == 20


# ── plan_browser_qa ──────────────────────────────────────────────────────


def test_backend_only_never_runs_browser_qa() -> None:
    plan = plan_browser_qa(tier="backend_only", needs_qa=True, managed_apps=["web"], deployed_apps=["quoting"])
    assert (plan.run_browser_qa, plan.human_merge_only) == (False, False)
    assert plan.reason_code == "backend_only"


def test_frontend_with_only_a_deployed_target_is_human_merge_only() -> None:
    plan = plan_browser_qa(tier="frontend", needs_qa=True, managed_apps=[], deployed_apps=["quoting"])
    assert (plan.run_browser_qa, plan.human_merge_only) == (False, True)
    assert plan.reason_code == "no_branch_stack"


def test_frontend_with_no_qa_config_is_human_merge_only() -> None:
    plan = plan_browser_qa(tier="frontend", needs_qa=False, managed_apps=[], deployed_apps=[])
    assert (plan.run_browser_qa, plan.human_merge_only) == (False, True)


def test_frontend_with_a_branch_booted_app_runs_browser_qa() -> None:
    plan = plan_browser_qa(tier="frontend", needs_qa=True, managed_apps=["web"], deployed_apps=["quoting"])
    assert (plan.run_browser_qa, plan.human_merge_only) == (True, False)
    assert plan.reason_code == "branch_stack"


def test_frontend_that_triage_exempted_from_qa_is_human_merge_only() -> None:
    plan = plan_browser_qa(tier="frontend", needs_qa=False, managed_apps=["web"], deployed_apps=[])
    assert (plan.run_browser_qa, plan.human_merge_only) == (False, True)
    assert plan.reason_code == "qa_not_requested"
