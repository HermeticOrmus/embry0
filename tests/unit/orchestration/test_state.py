from embry0.orchestration.state import (
    AgentOutputEntry,
    JobState,
    PipelineConfig,
    TriageAction,
    TriageDecision,
    last_value,
    make_pipeline_config,
    max_value,
    merge_qa_block,
)


def test_triage_action_values():
    assert TriageAction.PROCEED == "proceed"
    assert TriageAction.NEEDS_INFO == "needs_info"
    assert TriageAction.SPLIT == "split"


def test_pipeline_config_defaults():
    config = make_pipeline_config(
        sandbox_profile="python-3.12",
        agent_models={"developer": "claude-sonnet-4-6"},
        budget_usd=10.0,
    )
    assert config["reviewer_enabled"] is True
    assert config["max_feedback_loops"] == 5
    assert config["validator_modes"] == ["test", "lint", "typecheck"]


def test_triage_decision_structure():
    decision = TriageDecision(
        action="proceed",
        confidence=0.9,
        pipeline_template="standard",
        pipeline_config=PipelineConfig(
            sandbox_profile="python-3.12",
            agent_models={"developer": "claude-sonnet-4-6"},
            budget_usd=10.0,
        ),
        reasoning="High confidence, standard pipeline appropriate.",
    )
    assert decision["action"] == "proceed"
    assert decision["confidence"] == 0.9


def test_agent_output_entry():
    entry = AgentOutputEntry(
        agent_type="developer",
        is_error=False,
        output="Fixed the bug",
        cost_usd=0.42,
        duration_ms=15000,
    )
    assert entry["agent_type"] == "developer"
    assert entry["cost_usd"] == 0.42


def test_job_state_has_required_keys():
    annotations = JobState.__annotations__
    required_keys = [
        "job_id",
        "repo",
        "task",
        "sandbox_container_id",
        "pipeline_config",
        "triage_decision",
        "global_context",
        "repo_context",
        "agent_outputs",
        "errors",
        "current_stage",
        "total_cost_usd",
        "budget_overrun_usd",
        "pr_url",
        "result_summary",
        "user_retry_rounds",
    ]
    for key in required_keys:
        assert key in annotations, f"Missing key: {key}"


def test_job_state_declares_the_base_branch_source():
    """LangGraph's merge reducer drops undeclared keys silently, so an
    undeclared label would never reach the review node — which reads it to
    tell a resolved base branch from the module default."""
    assert "base_branch_source" in JobState.__annotations__


def test_job_state_declares_budget_overrun_mode_override():
    """#275: undeclared, the watcher's hard-stop pin would be dropped before
    run_agent_node could read it and the job would run under the fleet mode."""
    assert "budget_overrun_mode_override" in JobState.__annotations__


def test_job_state_has_cycle_guard_fields():
    """All cycle-guard fields exist: agent questions, triage questions, and user retries."""
    annotations = JobState.__annotations__
    assert "agent_question_rounds" in annotations
    assert "agent_questions_exhausted" in annotations
    assert "triage_question_rounds" in annotations
    assert "user_retry_rounds" in annotations


def test_qa_state_block_carries_boot_outcome_fields():
    from embry0.orchestration.state import QAStateBlock

    qa: QAStateBlock = {
        "needs_qa": True,
        "boot_outcome": "passed",
        "boot_duration_ms": 12345,
        "boot_attempts": 4,
        "boot_diagnostic_screenshot_path": "JOB1/1/screenshots/boot-timeout.png",
    }
    assert qa["boot_outcome"] == "passed"
    assert qa["boot_duration_ms"] == 12345
    assert qa["boot_attempts"] == 4
    assert qa["boot_diagnostic_screenshot_path"].endswith(".png")


# ---- RAV-1142: concurrent-write reducers ------------------------------------
#
# Both watcher-dispatched jobs of 2026-07-30 died with
# INVALID_CONCURRENT_GRAPH_UPDATE after their PRs were pushed: `current_stage`
# on the double-resume path, `qa` on the qa_rerun path. Any multi-writer key
# must carry a reducer; these tests pin the reducer semantics AND prove, on a
# real LangGraph fan-out, that a two-writers-one-superstep step folds instead
# of crashing.


def test_last_value_takes_update():
    assert last_value("developer", "review") == "review"
    assert last_value(None, "x") == "x"


def test_max_value_is_monotonic_and_none_safe():
    assert max_value(3, 5) == 5
    assert max_value(5, 3) == 5  # a stale concurrent writer cannot regress it
    assert max_value(None, 2) == 2
    assert max_value(2, None) == 2


def test_merge_qa_block_shallow_merges_and_none_clears():
    a = {"needs_qa": True, "failure_rounds": 1}
    b = {"final_status": "passed"}
    assert merge_qa_block(a, b) == {"needs_qa": True, "failure_rounds": 1, "final_status": "passed"}
    assert merge_qa_block(a, None) is None  # explicit clear wins
    assert merge_qa_block(None, b) == b


def test_concurrent_superstep_writes_fold_instead_of_crashing():
    """The regression: two nodes writing current_stage/qa/total_cost_usd in
    the SAME superstep. Pre-fix this raises InvalidUpdateError
    (INVALID_CONCURRENT_GRAPH_UPDATE) and kills the job."""
    from langgraph.graph import END, START, StateGraph

    from embry0.orchestration.state import JobState

    def writer_a(state: JobState) -> dict:
        return {"current_stage": "developer_retry", "qa": {"needs_qa": True}, "total_cost_usd": 5.0}

    def writer_b(state: JobState) -> dict:
        return {"current_stage": "review", "qa": {"final_status": "passed"}, "total_cost_usd": 4.0}

    g = StateGraph(JobState)
    g.add_node("writer_a", writer_a)
    g.add_node("writer_b", writer_b)
    g.add_edge(START, "writer_a")
    g.add_edge(START, "writer_b")  # fan-out: both run in one superstep
    g.add_edge("writer_a", END)
    g.add_edge("writer_b", END)

    result = g.compile().invoke({"job_id": "job-test", "total_cost_usd": 0.0})

    assert result["current_stage"] in ("developer_retry", "review")  # folded, not crashed
    assert result["qa"] == {"needs_qa": True, "final_status": "passed"}  # both writers preserved
    assert result["total_cost_usd"] == 5.0  # max, not last-write
