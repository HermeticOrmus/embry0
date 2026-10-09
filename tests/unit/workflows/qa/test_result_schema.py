import pytest
from pydantic import ValidationError

from embry0.workflows.qa.result_schema import (
    QAAcceptanceResult,
    QAAnomaly,
    QABootResult,
    QAReadyCheckResult,
    QAResult,
)


def test_minimal_passing_result():
    r = QAResult(
        schema_version=1,
        job_id="JOB",
        attempt_n=1,
        phase_reached="report",
        overall="passed",
        boot=QABootResult(
            command="x",
            duration_ms=1000,
            ready_checks=[QAReadyCheckResult(url="http://x/health", status=200, duration_ms=900)],
        ),
        acceptance_results=[QAAcceptanceResult(criterion="home loads", status="passed", evidence=["s/a.png"])],
        anomalies=[],
    )
    assert r.overall == "passed"


def test_overall_coerced_to_failed_when_acceptance_failed():
    """If any acceptance result is 'failed', overall is auto-coerced to
    'failed' (was: ValidationError; changed 2026-05-06 because the prior
    behavior rejected the agent's entire report and surfaced the run as
    infra_failure, hiding the real QA finding from the dashboard)."""
    r = QAResult(
        schema_version=1,
        job_id="J",
        attempt_n=1,
        phase_reached="report",
        overall="passed",
        boot=QABootResult(
            command="x", duration_ms=1, ready_checks=[QAReadyCheckResult(url="http://x", status=200, duration_ms=1)]
        ),
        acceptance_results=[QAAcceptanceResult(criterion="x", status="failed", evidence=[])],
        anomalies=[],
    )
    assert r.overall == "failed"


def test_overall_coerced_to_failed_when_crash_anomaly():
    """A crash anomaly forces overall=failed (auto-coerced)."""
    r = QAResult(
        schema_version=1,
        job_id="J",
        attempt_n=1,
        phase_reached="report",
        overall="passed",
        boot=QABootResult(
            command="x", duration_ms=1, ready_checks=[QAReadyCheckResult(url="http://x", status=200, duration_ms=1)]
        ),
        acceptance_results=[QAAcceptanceResult(criterion="x", status="passed", evidence=[])],
        anomalies=[QAAnomaly(category="crash", detail="OOM", evidence_paths=[])],
    )
    assert r.overall == "failed"


def test_overall_inconclusive_with_failed_acceptance_coerced_to_failed():
    """The historical 'inconclusive incompatible with any failed' case
    is now also auto-coerced rather than rejected."""
    r = QAResult(
        schema_version=1,
        job_id="J",
        attempt_n=1,
        phase_reached="report",
        overall="inconclusive",
        boot=QABootResult(
            command="x", duration_ms=1, ready_checks=[QAReadyCheckResult(url="http://x", status=200, duration_ms=1)]
        ),
        acceptance_results=[QAAcceptanceResult(criterion="x", status="failed", evidence=[])],
        anomalies=[],
    )
    assert r.overall == "failed"


def test_inconclusive_when_no_failed_only_inconclusive():
    r = QAResult(
        schema_version=1,
        job_id="J",
        attempt_n=1,
        phase_reached="report",
        overall="inconclusive",
        boot=QABootResult(
            command="x", duration_ms=1, ready_checks=[QAReadyCheckResult(url="http://x", status=200, duration_ms=1)]
        ),
        acceptance_results=[
            QAAcceptanceResult(criterion="x", status="inconclusive", evidence=[], notes="couldn't reach the page")
        ],
        anomalies=[],
    )
    assert r.overall == "inconclusive"


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        QAResult(
            schema_version=1,
            job_id="J",
            attempt_n=1,
            phase_reached="report",
            overall="passed",
            boot=QABootResult(
                command="x", duration_ms=1, ready_checks=[QAReadyCheckResult(url="http://x", status=200, duration_ms=1)]
            ),
            acceptance_results=[QAAcceptanceResult(criterion="x", status="passed", evidence=[])],
            anomalies=[],
            extra_field="oops",
        )


def test_scrub_unknown_keys_drops_agent_extras_recursively():
    """RAV-1400: agent-volunteered fields (observed live: e2e.note) must not
    turn a valid report into inconclusive — scrub, then strict-validate."""
    from embry0.workflows.qa.result_schema import scrub_unknown_keys

    raw = {
        "schema_version": 1,
        "job_id": "JOB",
        "attempt_n": 1,
        "phase_reached": "report",
        "overall": "passed",
        "vibe": "immaculate",  # unknown top-level
        "boot": {
            "command": "x",
            "duration_ms": 1000,
            "made_up": True,  # unknown nested
            "ready_checks": [{"url": "http://x/health", "status": 200, "duration_ms": 900, "extra": 1}],
        },
        "e2e": {"ran": False, "note": "no e2e command declared in qa.yaml"},  # the live case
        "acceptance_results": [{"criterion": "home loads", "status": "passed", "evidence": [], "hunch": "fine"}],
        "anomalies": [],
    }
    dropped: list[str] = []
    scrubbed = scrub_unknown_keys(QAResult, raw, dropped)
    r = QAResult.model_validate(scrubbed)
    assert r.overall == "passed"
    assert r.e2e is not None and r.e2e.ran is False
    assert sorted(dropped) == [
        "acceptance_results[].hunch",
        "boot.made_up",
        "boot.ready_checks[].extra",
        "e2e.note",
        "vibe",
    ]


def test_scrub_keeps_type_errors_loud():
    from embry0.workflows.qa.result_schema import scrub_unknown_keys

    raw = {
        "schema_version": 1,
        "job_id": "JOB",
        "attempt_n": "not-an-int",
        "phase_reached": "report",
        "overall": "passed",
        "boot": None,
        "acceptance_results": [],
        "anomalies": [],
    }
    scrubbed = scrub_unknown_keys(QAResult, raw, [])
    with pytest.raises(ValidationError):
        QAResult.model_validate(scrubbed)
