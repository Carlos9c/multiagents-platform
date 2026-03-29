from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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


class ValidationTaskContext(BaseModel):
    task_id: int
    project_id: int
    title: str
    description: str | None = None
    summary: str | None = None
    objective: str | None = None
    acceptance_criteria: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None
    task_type: str | None = None
    planning_level: str | None = None
    executor_type: str | None = None


class ValidationExecutionContext(BaseModel):
    execution_run_id: int
    execution_status: str
    decision: str
    summary: str
    details: str | None = None
    rejection_reason: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    output_snapshot: str | None = None
    execution_agent_sequence: list[str] = Field(default_factory=list)


class ValidationRequestContext(BaseModel):
    workspace_path: str | None = None
    source_path: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)
    relevant_files: list[str] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    related_task_ids: list[int] = Field(default_factory=list)


class ValidationEvidenceItem(BaseModel):
    evidence_id: str = Field(..., min_length=3)
    evidence_kind: str = Field(..., min_length=3)
    media_type: str | None = None
    representation_kind: str = Field(..., min_length=3)
    source: str = Field(..., min_length=3)

    logical_name: str | None = None
    path: str | None = None
    artifact_id: int | None = None
    change_type: str | None = None

    content_text: str | None = None
    content_summary: str | None = None
    structured_content: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationEvidencePackage(BaseModel):
    evidence_items: list[ValidationEvidenceItem] = Field(default_factory=list)


class TaskValidationInput(BaseModel):
    intent: ResolvedValidationIntent
    task: ValidationTaskContext
    execution: ValidationExecutionContext
    request_context: ValidationRequestContext
    evidence_package: ValidationEvidencePackage
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
