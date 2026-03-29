from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


EXECUTION_DECISION_COMPLETED = "completed"
EXECUTION_DECISION_PARTIAL = "partial"
EXECUTION_DECISION_FAILED = "failed"
EXECUTION_DECISION_REJECTED = "rejected"

VALID_EXECUTION_DECISIONS = {
    EXECUTION_DECISION_COMPLETED,
    EXECUTION_DECISION_PARTIAL,
    EXECUTION_DECISION_FAILED,
    EXECUTION_DECISION_REJECTED,
}

CHANGE_TYPE_CREATED = "created"
CHANGE_TYPE_MODIFIED = "modified"
CHANGE_TYPE_DELETED = "deleted"

VALID_CHANGE_TYPES = {
    CHANGE_TYPE_CREATED,
    CHANGE_TYPE_MODIFIED,
    CHANGE_TYPE_DELETED,
}


class RelatedTaskSummary(BaseModel):
    task_id: int
    title: str
    status: str
    summary: str | None = None


class ProjectExecutionContext(BaseModel):
    project_id: int
    source_path: str
    workspace_path: str
    relevant_files: list[str] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    related_tasks: list[RelatedTaskSummary] = Field(default_factory=list)


class ExecutionRequest(BaseModel):
    task_id: int
    project_id: int
    execution_run_id: int

    task_title: str
    task_description: str | None = None
    task_summary: str | None = None
    objective: str | None = None
    acceptance_criteria: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None

    executor_type: str

    success_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)

    allowed_paths: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)

    context: ProjectExecutionContext


class ChangedFile(BaseModel):
    path: str
    change_type: Literal["created", "modified", "deleted"]


class CommandExecution(BaseModel):
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ExecutionEvidence(BaseModel):
    changed_files: list[ChangedFile] = Field(default_factory=list)
    commands: list[CommandExecution] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    artifacts_created: list[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    task_id: int
    decision: Literal["completed", "partial", "failed", "rejected"]
    summary: str
    details: str | None = None
    rejection_reason: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    output_snapshot: str | None = None
    execution_agent_sequence: list[str] = Field(default_factory=list)
    evidence: ExecutionEvidence = Field(default_factory=ExecutionEvidence)
