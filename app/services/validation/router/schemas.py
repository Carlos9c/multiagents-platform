from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


VALIDATOR_KEY_CODE_TASK = "code_task_validator"

VALIDATION_DISCIPLINE_CODE = "code"

VALIDATION_MODE_POST_EXECUTION = "post_execution"
VALIDATION_MODE_TERMINAL_FAILURE = "terminal_failure"
VALIDATION_MODE_TERMINAL_REJECTION = "terminal_rejection"


class ValidationRoutingTaskContext(BaseModel):
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


class ValidationRoutingExecutionSummary(BaseModel):
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


class ValidationRoutingEvidenceSummary(BaseModel):
    changed_file_paths: list[str] = Field(default_factory=list)
    command_count: int = 0
    artifact_refs: list[str] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)
    relevant_files: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    related_task_ids: list[int] = Field(default_factory=list)


class ValidationRoutingInput(BaseModel):
    task: ValidationRoutingTaskContext
    execution: ValidationRoutingExecutionSummary
    evidence: ValidationRoutingEvidenceSummary


class ValidationRoutingDecision(BaseModel):
    validator_key: str = Field(..., min_length=3)
    discipline: str = Field(..., min_length=3)
    validation_mode: Literal[
        "post_execution",
        "terminal_failure",
        "terminal_rejection",
    ]
    requires_workspace: bool = False
    requires_file_reading: bool = False
    requires_changed_files: bool = False
    requires_command_results: bool = False
    requires_artifacts: bool = False
    requires_output_snapshot: bool = False
    requires_execution_agent_sequence: bool = False
    require_manual_review_if_evidence_missing: bool = False
    validation_focus: list[str] = Field(default_factory=list)
    routing_rationale: str
    open_questions: list[str] = Field(default_factory=list)

    @classmethod
    def default_code_route(
        cls,
        *,
        validation_mode: Literal[
            "post_execution",
            "terminal_failure",
            "terminal_rejection",
        ],
        routing_rationale: str,
        validation_focus: list[str] | None = None,
        open_questions: list[str] | None = None,
    ) -> "ValidationRoutingDecision":
        return cls(
            validator_key=VALIDATOR_KEY_CODE_TASK,
            discipline=VALIDATION_DISCIPLINE_CODE,
            validation_mode=validation_mode,
            requires_workspace=True,
            requires_file_reading=True,
            requires_changed_files=True,
            requires_command_results=True,
            requires_artifacts=True,
            requires_output_snapshot=True,
            requires_execution_agent_sequence=True,
            require_manual_review_if_evidence_missing=True,
            validation_focus=validation_focus or [
                "acceptance_criteria_alignment",
                "scope_completion",
                "repository_changes",
                "constraint_compliance",
            ],
            routing_rationale=routing_rationale,
            open_questions=open_questions or [],
        )