from embry0.workflows.issue_to_pr.graph import IssueToprWorkflow


def test_workflow_has_name():
    wf = IssueToprWorkflow()
    assert wf.name == "issue-to-pr"
    assert wf.description


def test_workflow_compiles():
    wf = IssueToprWorkflow()
    graph = wf.compile()
    assert graph is not None
    assert hasattr(graph, "ainvoke")


def test_workflow_graph_has_expected_nodes():
    wf = IssueToprWorkflow()
    graph = wf.compile()
    node_names = set(graph.nodes.keys())
    assert "init" in node_names
    assert "triage" in node_names
    assert "developer" in node_names
    assert "review" in node_names
    assert "retry" in node_names
    assert "max_retries" in node_names


def test_generic_agent_node_wired():
    """RAV-604: the generic node exists and every dispatch seam can reach it."""
    wf = IssueToprWorkflow()
    graph = wf.compile()
    g = graph.get_graph()
    assert "generic_agent" in set(g.nodes)
    edges = {(e.source, e.target) for e in g.edges}
    # Reachable from plan_route, from its own chaining seam, and onward to
    # finalize_output (the research-default shape: triage→research→output).
    assert ("plan_route", "generic_agent") in edges
    assert ("generic_agent", "generic_agent") in edges
    assert ("generic_agent", "finalize_output") in edges
    # review → generic_agent is now dynamic Command routing (RAV-1152:
    # review's conditional edge was removed), so it no longer appears in
    # the static edge set.
    assert ("qa_failure_bookkeeping", "generic_agent") in edges


def test_issue_to_pr_graph_uses_multi_app_qa():
    wf = IssueToprWorkflow()
    graph = wf.compile()
    nodes = set(graph.get_graph().nodes)
    assert "init_orchestrator" in nodes
    assert "orchestrate_qa" in nodes
    assert "qa_report" in nodes
    assert "init_qa" not in nodes
    assert "boot_qa" not in nodes
    assert "qa" not in nodes
    assert "qa_failure_bookkeeping" in nodes
    assert "qa_exhausted" in nodes


def test_mixed_command_and_conditional_edges_fan_out():
    """RAV-1152: documents WHY triage/developer/review must never carry
    conditional edges — a node with both a Command return and conditional
    edges activates BOTH targets in one superstep. This is the mechanism
    behind the INVALID_CONCURRENT_GRAPH_UPDATE crashes (pre-#95) and the
    spurious max_retries pause + duplicate review run (RAV-1152). If this
    test ever FAILS (i.e. langgraph stops fanning out), the Command-only
    constraint can be revisited."""
    from typing import TypedDict

    import pytest as _pytest
    from langgraph.errors import InvalidUpdateError
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    class S(TypedDict, total=False):
        marker: str

    def node_a(state):
        return Command(goto="C", update={"marker": "A"})

    g = StateGraph(S)
    g.add_node("A", node_a)
    g.add_node("B", lambda s: {"marker": "B"})
    g.add_node("C", lambda s: {"marker": "C"})
    g.add_edge(START, "A")
    g.add_conditional_edges("A", lambda s: "B", {"B": "B", "C": "C"})
    g.add_edge("B", END)
    g.add_edge("C", END)

    # Both B and C run → two writes to the plain key in one superstep.
    with _pytest.raises(InvalidUpdateError):
        g.compile().invoke({})


def test_command_routing_nodes_have_no_conditional_edges():
    """The structural guarantee itself: triage/developer/review appear as
    conditional-edge SOURCES nowhere in the compiled graph — their only
    dispatch is the Command they return."""
    import inspect

    import embry0.workflows.issue_to_pr.graph as graph_mod

    src = inspect.getsource(graph_mod.IssueToprWorkflow.compile)
    for node in ('"triage"', '"developer"', '"review"', '"verify"'):
        assert f"add_conditional_edges(\n            {node}" not in src
        assert f"add_conditional_edges({node}" not in src


# ---- SH-P4.7 (#304): concurrent writes on the REAL compiled graph -----------
#
# job-6ec3720503d1 (RAV-1109, `current_stage`) and job-19a9f11c505d (RAV-1135,
# `qa`) died with INVALID_CONCURRENT_GRAPH_UPDATE after their PRs existed.
# #95 put reducers on the multi-writer keys and #100 removed the mixed
# Command + conditional-edge fan-out. test_state.py pins the reducers on a
# synthetic two-node graph; these pin them on IssueToprWorkflow's own schema
# and wiring. What they do NOT pin is the double-dispatch SHAPE: production
# never emits `Command(goto=[a, b])` — it is only the cheapest way to force
# two writers into one superstep here. The shape is guarded structurally by
# test_command_routing_nodes_have_no_conditional_edges above, and "a new key
# without a reducer" by tests/unit/orchestration/test_state_channels.py.


def _compile_with_fan_out(monkeypatch, *, state_schema=None):
    """Compile the real issue-to-PR graph with stub node bodies in which
    `review` is forced to dispatch `retry` AND `max_retries` in one superstep
    (the EFFECT of the 2026-07 double-dispatch, not its mechanism) and both
    write the keys that crashed."""
    from langgraph.graph import END
    from langgraph.types import Command

    import embry0.workflows.issue_to_pr.graph as graph_mod

    async def _init(state):
        return {"current_stage": "init"}

    async def _triage(state):
        return Command(goto="review", update={"current_stage": "triage_complete"})

    async def _review(state):
        return Command(goto=["retry", "max_retries"], update={"current_stage": "review_failed"})

    async def _retry(state):
        return {"current_stage": "developer_retry", "qa": {"needs_qa": True}, "total_cost_usd": 5.0}

    async def _max_retries(state):
        return {"current_stage": "max_retries", "qa": {"final_status": "failed"}, "total_cost_usd": 4.0}

    async def _developer(state):
        return Command(goto=END, update={"current_stage": "developer_complete"})

    for name, stub in {
        "init_node": _init,
        "triage_node": _triage,
        "review_node": _review,
        "retry_node": _retry,
        "max_retries_node": _max_retries,
        "developer_node": _developer,
    }.items():
        monkeypatch.setattr(graph_mod, name, stub)
    if state_schema is not None:
        monkeypatch.setattr(graph_mod, "JobState", state_schema)
    return graph_mod.IssueToprWorkflow().compile()


async def test_real_graph_folds_two_writers_in_one_superstep(monkeypatch):
    graph = _compile_with_fan_out(monkeypatch)

    result = await graph.ainvoke({"job_id": "job-test", "total_cost_usd": 0.0})

    assert result["current_stage"] == "developer_complete"  # ran past the fan-out
    assert result["qa"] == {"needs_qa": True, "final_status": "failed"}  # both writers kept
    assert result["total_cost_usd"] == 5.0  # max, never regressed by the stale writer


async def test_real_graph_crashes_the_same_way_without_the_reducers(monkeypatch):
    """The pre-#95 schema on today's wiring reproduces the production error
    verbatim — proof the test above exercises the crash, not a no-op."""
    from typing import TypedDict, get_type_hints

    import pytest
    from langgraph.errors import InvalidUpdateError

    from embry0.orchestration.state import JobState

    plain = TypedDict("plain", get_type_hints(JobState), total=False)  # type: ignore[operator]  # Annotated stripped
    graph = _compile_with_fan_out(monkeypatch, state_schema=plain)

    with pytest.raises(InvalidUpdateError, match="Can receive only one value per step"):
        await graph.ainvoke({"job_id": "job-test", "total_cost_usd": 0.0})


def test_crash_report_keys_are_reducer_channels_in_the_real_graph():
    """The keys the crash reports named (plus their sibling scalars) must be
    reducer channels in the compiled graph — a plain LastValue channel is
    what raises on a second write."""
    from langgraph.channels.binop import BinaryOperatorAggregate

    channels = IssueToprWorkflow().compile().channels
    for key in ("current_stage", "qa", "error_code", "retry_count", "total_cost_usd", "route_cursor"):
        assert isinstance(channels[key], BinaryOperatorAggregate), key
