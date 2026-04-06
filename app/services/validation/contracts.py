from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.execution_engine.contracts import ExecutionRequest, ExecutionResult

VALIDATION_DISCIPLINE_CODE = "code"

VALIDATION_MODE_POST_EXECUTION = "post_execution"
VALIDATION_MODE_TERMINAL_FAILURE = "terminal_failure"
VALIDATION_MODE_TERMINAL_REJECTION = "terminal_rejection"

VALIDATION_DECISION_COMPLETED = "completed"
VALIDATION_DECISION_PARTIAL = "partial"
VALIDATION_DECISION_FAILED = "failed"
VALIDATION_DECISION_MANUAL_REVIEW = "manual_review"


class ResolvedValidationIntent(BaseModel):
    validator_key: str = Field(..., min_length=3)
    discipline: str = Field(..., min_length=3)
    validation_mode: Literal[
        "post_execution",
        "terminal_failure",
        "terminal_rejection",
    ]
    requires_workspace: bool = False
    requires_artifacts: bool = False
    requires_changed_files: bool = False
    requires_commands: bool = False
    requires_execution_context: bool = False
    requires_output_snapshot: bool = False
    requires_agent_sequence: bool = False
    requires_file_reading: bool = False
    notes: list[str] = Field(default_factory=list)


class TaskValidationInput(BaseModel):
    execution_request: ExecutionRequest
    execution_result: ExecutionResult
    intent: ResolvedValidationIntent | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationFinding(BaseModel):
    severity: Literal["info", "warning", "error"]
    message: str
    code: str | None = None
    file_path: str | None = None


class ValidationResult(BaseModel):
    validator_key: str = Field(..., min_length=3)
    discipline: str = Field(..., min_length=3)
    decision: Literal[
        "completed",
        "partial",
        "failed",
        "manual_review",
    ]
    summary: str
    findings: list[ValidationFinding] = Field(default_factory=list)
    validated_scope: str | None = None
    missing_scope: str | None = None
    blockers: list[str] = Field(default_factory=list)
    manual_review_required: bool = False
    final_task_status: str | None = None
    artifacts_created: list[str] = Field(default_factory=list)
    validated_evidence_ids: list[str] = Field(default_factory=list)
    unconsumed_evidence_ids: list[str] = Field(default_factory=list)
    followup_validation_required: bool = False
    recommended_next_validator_keys: list[str] = Field(default_factory=list)
    partial_validation_summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
