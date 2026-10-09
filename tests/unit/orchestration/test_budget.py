from embry0.orchestration.budget import check_budget


def test_within_budget():
    result = check_budget(current_cost=5.0, max_budget=10.0, overrun_mode="soft")
    assert result.allowed is True
    assert result.remaining == 5.0


def test_over_budget_soft_stop():
    result = check_budget(current_cost=12.0, max_budget=10.0, overrun_mode="soft")
    assert result.allowed is True
    assert result.overrun == 2.0


def test_over_budget_hard_stop():
    result = check_budget(current_cost=12.0, max_budget=10.0, overrun_mode="hard")
    assert result.allowed is False
    assert result.overrun == 2.0


def test_zero_budget():
    result = check_budget(current_cost=0.0, max_budget=10.0, overrun_mode="soft")
    assert result.allowed is True
    assert result.remaining == 10.0


def test_resolve_job_cap_prefers_pipeline_config():
    from embry0.orchestration.budget import resolve_job_cap

    state = {"pipeline_config": {"budget_usd": 42.0}, "max_budget_usd": 7.0}
    assert resolve_job_cap(state) == 42.0


def test_resolve_job_cap_falls_back_to_job_override_before_triage():
    from embry0.orchestration.budget import resolve_job_cap

    assert resolve_job_cap({"max_budget_usd": 7.0}) == 7.0


def test_resolve_job_cap_default():
    from embry0.orchestration.budget import DEFAULT_JOB_BUDGET_USD, resolve_job_cap

    assert resolve_job_cap({}) == DEFAULT_JOB_BUDGET_USD


def test_resolve_job_cap_is_bounded_by_the_fleet_ceiling():
    from embry0.orchestration.budget import resolve_job_cap

    state = {"pipeline_config": {"budget_usd": 50.0}}
    assert resolve_job_cap(state, ceiling=4.0) == 4.0
    # A per-job choice under the ceiling is left alone.
    assert resolve_job_cap({"pipeline_config": {"budget_usd": 2.0}}, ceiling=4.0) == 2.0
    # 0 means "no ceiling configured".
    assert resolve_job_cap(state, ceiling=0.0) == 50.0
