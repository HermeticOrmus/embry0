def test_job_json_payload_carries_guardrails():
    """EMB-31: resolved guardrails ride job.json for the in-sandbox agent."""
    from embry0.workflows.qa.qa_yaml_resolve import ResolvedAppConfig
    from embry0.workflows.qa.subtask_state import _build_job_json_payload

    resolved = ResolvedAppConfig(
        app_name="hub",
        boot_command="npm start",
        frontend_url="http://localhost:3000",
        mode="process",
        sandbox_profile="qa-external",
        ready_checks=[],
        boot_timeout_seconds=60,
        seed_command=None,
        e2e=None,
        acceptance_criteria=["home loads"],
        guardrails=["Never click Send"],
    )
    payload = _build_job_json_payload(
        sub_job_id="J__hub",
        attempt_n=1,
        qa_yaml={"mode": "process", "frontend_url": "http://localhost:3000"},
        resolved=resolved,
        sandbox_token="tok",
    )
    assert payload["guardrails"] == ["Never click Send"]


# -------- browser_backend rides into job.json (#229 / #232) --------


def test_job_json_carries_resolved_browser_backend():
    from embry0.workflows.qa.qa_yaml_resolve import ResolvedAppConfig
    from embry0.workflows.qa.subtask_state import _build_job_json_payload, _synth_v1_qa_yaml

    resolved = ResolvedAppConfig(
        app_name="web",
        boot_command="npm run dev",
        frontend_url="http://localhost:3000",
        mode="process",
        sandbox_profile="qa-jvm",
        ready_checks=[],
        boot_timeout_seconds=300,
        seed_command=None,
        e2e=None,
        acceptance_criteria=["page loads"],
        browser_backend="playwright",
    )
    payload = _build_job_json_payload(
        sub_job_id="sub-1",
        attempt_n=1,
        qa_yaml=_synth_v1_qa_yaml(resolved),
        resolved=resolved,
        sandbox_token="tok",
    )
    assert payload["browser_backend"] == "playwright"


def test_resolved_app_config_browser_backend_defaults_to_playwright():
    """Constructed without the field (older call sites, tests) the resolved
    config still lands on the default backend."""
    from embry0.workflows.qa.qa_yaml_resolve import ResolvedAppConfig

    resolved = ResolvedAppConfig(
        app_name="web",
        boot_command="x",
        frontend_url="http://localhost:1",
        mode="process",
        sandbox_profile="slim",
        ready_checks=[],
        boot_timeout_seconds=60,
        seed_command=None,
        e2e=None,
        acceptance_criteria=[],
    )
    assert resolved.browser_backend == "playwright"


def test_synth_v1_qa_yaml_carries_browser_backend():
    """qa_node reads the backend off qa_yaml_parsed — _synth must carry it."""
    from embry0.workflows.qa.qa_yaml_resolve import ResolvedAppConfig
    from embry0.workflows.qa.subtask_state import _synth_v1_qa_yaml

    resolved = ResolvedAppConfig(
        app_name="web",
        boot_command="x",
        frontend_url="http://localhost:1",
        mode="process",
        sandbox_profile="slim",
        ready_checks=[],
        boot_timeout_seconds=60,
        seed_command=None,
        e2e=None,
        acceptance_criteria=[],
        browser_backend="vibium",
    )
    assert _synth_v1_qa_yaml(resolved)["browser_backend"] == "vibium"
