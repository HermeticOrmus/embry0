"""Watcher analysis jobs carry their own hard per-job cap (#275)."""

from types import SimpleNamespace

import pytest

from embry0.services.watcher_budget import (
    DEFAULT_WATCHER_JOB_BUDGET_USD,
    WatcherJobBudget,
    resolve_watcher_job_budget,
)


def test_cap_comes_from_the_watcher_setting():
    budget = resolve_watcher_job_budget(SimpleNamespace(watcher_job_budget_usd=1.5))
    assert budget == WatcherJobBudget(cap_usd=1.5)


def test_per_repo_config_outranks_the_setting():
    """#297 will hand a watched repo's config row in here."""
    budget = resolve_watcher_job_budget(
        SimpleNamespace(watcher_job_budget_usd=1.5),
        repo_config={"job_budget_usd": 0.75},
    )
    assert budget.cap_usd == 0.75


def test_repo_config_without_a_cap_falls_back_to_the_setting():
    budget = resolve_watcher_job_budget(SimpleNamespace(watcher_job_budget_usd=1.5), repo_config={})
    assert budget.cap_usd == 1.5


@pytest.mark.parametrize("bad", [0, -1.0, None, "abc", float("inf"), float("-inf"), float("nan"), "inf", "nan"])
def test_an_unusable_cap_never_means_uncapped(bad):
    """0 means 'unlimited' elsewhere in budget config — not for unattended jobs."""
    assert resolve_watcher_job_budget(SimpleNamespace(watcher_job_budget_usd=bad)).cap_usd == (
        DEFAULT_WATCHER_JOB_BUDGET_USD
    )
    assert resolve_watcher_job_budget(SimpleNamespace()).cap_usd == DEFAULT_WATCHER_JOB_BUDGET_USD


def test_a_non_finite_repo_cap_falls_through_to_the_setting():
    budget = resolve_watcher_job_budget(
        SimpleNamespace(watcher_job_budget_usd=1.5),
        repo_config={"job_budget_usd": float("inf")},
    )
    assert budget.cap_usd == 1.5


def test_the_default_cap_is_two_dollars():
    assert DEFAULT_WATCHER_JOB_BUDGET_USD == 2.0


def test_pipeline_config_pins_the_cap_and_hard_mode():
    assert WatcherJobBudget(cap_usd=2.0).as_pipeline_config() == {
        "max_budget_usd": 2.0,
        "budget_overrun_mode": "hard",
    }
