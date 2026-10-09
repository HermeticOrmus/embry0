"""Pydantic schema for the QA agent's result.json output.

The agent writes this file at the end of its run. The orchestrator
parses + validates it before persisting summary fields to JobState
and uploading to MinIO. Validation failures land an `inconclusive`
attempt with `result_json_invalid` in the exit reason.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QAReadyCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    status: int
    duration_ms: int


class QABootResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    duration_ms: int
    ready_checks: list[QAReadyCheckResult] = Field(min_length=1)


class QASeedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ran: bool
    command: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    note: str | None = None


class QAE2EResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ran: bool
    command: str | None = None
    exit_code: int | None = None
    tests: dict[str, int] = Field(default_factory=dict)
    junit_artifact: str | None = None


class QAAcceptanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str
    status: Literal["passed", "failed", "inconclusive"]
    evidence: list[str] = Field(default_factory=list)
    notes: str | None = None
    console_errors: list[str] = Field(default_factory=list)
    network_failures: list[dict[str, Any]] = Field(default_factory=list)
    log_excerpts: list[dict[str, Any]] = Field(default_factory=list)


class QAAnomaly(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["console_error", "network_error", "unexpected_state", "crash", "guardrail_violation"]
    detail: str
    evidence_paths: list[str] = Field(default_factory=list)
    ts: str | None = None


class QAResult(BaseModel):
    """Top-level result.json contract."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    job_id: str
    attempt_n: int
    phase_reached: Literal["boot", "seed", "e2e", "exploratory", "report"]
    overall: Literal["passed", "failed", "inconclusive"]

    boot: QABootResult | None = None
    seed: QASeedResult | None = None
    e2e: QAE2EResult | None = None
    acceptance_results: list[QAAcceptanceResult]
    anomalies: list[QAAnomaly]

    @model_validator(mode="after")
    def _overall_consistent_with_findings(self) -> QAResult:
        # Auto-coerce overall to "failed" when the agent's anomalies /
        # acceptance_results are inconsistent with the reported overall.
        # Earlier behavior raised ValueError, which rejected the agent's
        # entire report and surfaced the run as `infra_failure` even though
        # the agent succeeded — observed 2026-05-06 on the dashboard app
        # (agent emitted a crash anomaly but set overall='inconclusive';
        # Pydantic rejected, dashboard showed infra_failure instead of
        # the agent's actual finding). Coerce + log so the dashboard
        # reflects the QA finding while the inconsistency is visible.
        import logging  # local import — schema module stays import-light

        log = logging.getLogger(__name__)

        crash_present = any(a.category == "crash" for a in self.anomalies)
        any_failed = any(r.status == "failed" for r in self.acceptance_results)

        if crash_present and self.overall != "failed":
            log.warning(
                "qa_result_overall_coerced_to_failed",
                extra={"reason": "crash_anomaly_present", "agent_overall": self.overall},
            )
            self.overall = "failed"

        if any_failed and self.overall != "failed":
            log.warning(
                "qa_result_overall_coerced_to_failed",
                extra={"reason": "failed_acceptance_result", "agent_overall": self.overall},
            )
            self.overall = "failed"

        # Belt-and-braces: this branch was unreachable above (caught by the
        # any_failed coerce) — keep as a defensive no-op so future edits
        # to the rules above don't silently regress the invariant.
        if self.overall == "inconclusive" and any_failed:
            self.overall = "failed"

        return self


def scrub_unknown_keys(
    model_cls: type[BaseModel],
    data: Any,
    dropped: list[str] | None = None,
    _path: str = "",
) -> Any:
    """Drop keys the schema doesn't declare, recursively (RAV-1400).

    The models above stay ``extra="forbid"`` — this pre-validation scrub is
    the tolerant layer for AGENT-authored result.json (same policy as the
    RAV-1250 triage extras filter): an agent volunteering a field
    (observed: ``e2e.note``) must not turn an otherwise-valid report into
    an ``inconclusive`` attempt. Unknown keys are collected into
    ``dropped`` (as dotted paths) for logging; wrong TYPES on known keys
    still fail validation loudly.
    """
    from typing import Union, get_args, get_origin

    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        field = model_cls.model_fields.get(key)
        if field is None:
            if dropped is not None:
                dropped.append(f"{_path}{key}")
            continue
        ann = field.annotation

        def _nested_model(a: Any) -> type[BaseModel] | None:
            if isinstance(a, type) and issubclass(a, BaseModel):
                return a
            origin = get_origin(a)
            if (
                origin is Union
                or str(type(a)) == "<class 'types.UnionType'>"
                or str(origin) == "<class 'types.UnionType'>"
            ):
                for arg in get_args(a):
                    if isinstance(arg, type) and issubclass(arg, BaseModel):
                        return arg
            return None

        nested = _nested_model(ann)
        if nested is not None and isinstance(value, dict):
            out[key] = scrub_unknown_keys(nested, value, dropped, f"{_path}{key}.")
            continue
        origin = get_origin(ann)
        if origin in (list, tuple) and isinstance(value, list):
            args = get_args(ann)
            item_model = _nested_model(args[0]) if args else None
            if item_model is not None:
                out[key] = [
                    scrub_unknown_keys(item_model, item, dropped, f"{_path}{key}[].")
                    if isinstance(item, dict)
                    else item
                    for item in value
                ]
                continue
        out[key] = value
    return out
