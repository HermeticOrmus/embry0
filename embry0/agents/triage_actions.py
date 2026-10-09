"""Pydantic models for the structured tool calls triage emits.

Triage's role expanded over time: pipeline routing decisions, ask_user
prompts, and (Phase 5) QA decisions and QA-failure handling. Each
distinct action is a Pydantic model that the agent SDK serializes as a
tool call schema.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SetQADecision(BaseModel):
    """Triage's QA decision for issue→PR jobs."""

    model_config = ConfigDict(extra="forbid")

    needs_qa: bool
    reason: str = Field(min_length=1, max_length=500)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)


class RetryDeveloper(BaseModel):
    """After a QA failure, ask developer to fix with specific guidance."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=10, max_length=4000)
    focus_files: list[str] = Field(default_factory=list, max_length=20)


class RerunQA(BaseModel):
    """After a QA failure, declare it environmental/flaky and rerun unchanged."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class AskUser(BaseModel):
    """After a QA failure, escalate to the user via the existing ask_user channel."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=10, max_length=2000)


# Discriminated union for the QA failure response — exactly one action.
# Consumers parse incoming payloads via ``pydantic.TypeAdapter(QAFailureAction).validate_python(...)``.
QAFailureAction = RetryDeveloper | RerunQA | AskUser


def clamp_advisory_strings(model_cls: type[BaseModel], payload: dict[str, Any]) -> dict[str, Any]:
    """Truncate over-long string fields to the model's declared max_length.

    RAV-1248: the advisory strings on these models (reason / prompt /
    question) are human-facing context, not control flow — a model that
    explains itself past the field cap must not turn a well-formed action
    into a ValidationError (which the QA-failure handler treats as
    job-terminal, and the set_qa_decision extractors treat as "no
    decision"). Call before ``model_validate``; genuinely missing or
    too-short fields still fail validation as before.
    """
    clamped = dict(payload)
    for name, field in model_cls.model_fields.items():
        value = clamped.get(name)
        if not isinstance(value, str):
            continue
        max_len = next(
            (m.max_length for m in field.metadata if getattr(m, "max_length", None) is not None),
            None,
        )
        if max_len is not None and len(value) > max_len:
            clamped[name] = value[: max_len - 1] + "…"
    return clamped
