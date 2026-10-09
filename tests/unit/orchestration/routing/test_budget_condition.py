"""check_budget routing condition — the soft-mode stop signal (#246)."""

from embry0.orchestration.routing.conditions import check_budget


def test_within_budget():
    assert check_budget({"total_cost_usd": 5.0, "pipeline_config": {"budget_usd": 10.0}}) == "within_budget"


def test_over_budget_by_direct_comparison():
    assert check_budget({"total_cost_usd": 11.0, "pipeline_config": {"budget_usd": 10.0}}) == "over_budget"


def test_recorded_overrun_wins_over_the_direct_comparison():
    """The agent node resolves the cap against the fleet-wide ceiling, which
    this pure routing function cannot read — trust its verdict."""
    state = {"total_cost_usd": 5.0, "pipeline_config": {"budget_usd": 10.0}, "budget_overrun_usd": 1.0}
    assert check_budget(state) == "over_budget"


def test_default_cap_when_triage_has_not_run():
    assert check_budget({"total_cost_usd": 11.0}) == "over_budget"
    assert check_budget({"total_cost_usd": 9.0}) == "within_budget"
