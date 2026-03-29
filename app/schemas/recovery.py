from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator, ConfigDict


RecoveryAction = Literal[
    "reatomize",
    "insert_followup",
    "manual_review",
]

RecoveryConfidence = Literal[
    "low",
    "medium",
    "high",
]

RecoveryTaskType = Literal[
    "implementation",
    "test",
    "documentation",
    "configuration",
    "refactor",
]

RecoveryTaskPriority = Literal[
    "low",
    "medium",
    "high",
]


class RecoveryTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., min_length=3)
    description: str = Field(..., min_length=10)
    objective: str | None = None
    implementation_notes: str | None = None
    acceptance_criteria: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None
    task_type: RecoveryTaskType = "implementation"
    priority: RecoveryTaskPriority = "medium"

    @model_validator(mode="after")
    def normalize_fields(self) -> "RecoveryTaskCreate":
        if self.objective is not None:
            self.objective = self.objective.strip() or None
        if self.implementation_notes is not None:
            self.implementation_notes = self.implementation_notes.strip() or None
        if self.acceptance_criteria is not None:
            self.acceptance_criteria = self.acceptance_criteria.strip() or None
        if self.technical_constraints is not None:
            self.technical_constraints = self.technical_constraints.strip() or None
        if self.out_of_scope is not None:
            self.out_of_scope = self.out_of_scope.strip() or None
        return self


class RecoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_task_id: int = Field(..., gt=0)
    source_run_id: int = Field(..., gt=0)

    action: RecoveryAction
    confidence: RecoveryConfidence

    reason: str = Field(..., min_length=10)
    covered_gap_summary: str = Field(..., min_length=10)

    execution_guidance: str | None = None
    evaluation_guidance: str | None = None

    requires_manual_review: bool = False
    still_blocks_progress: bool = True

    created_tasks: list[RecoveryTaskCreate] = Field(default_factory=list)

    decision_origin: str | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> "RecoveryDecision":
        if self.execution_guidance is not None:
            self.execution_guidance = self.execution_guidance.strip() or None
        if self.evaluation_guidance is not None:
            self.evaluation_guidance = self.evaluation_guidance.strip() or None
        if self.decision_origin is not None:
            self.decision_origin = self.decision_origin.strip() or None

        if self.action == "manual_review":
            if not self.requires_manual_review:
                raise ValueError(
                    "action='manual_review' requires requires_manual_review=true."
                )
            if self.created_tasks:
                raise ValueError(
                    "action='manual_review' must not include created_tasks."
                )
        else:
            if self.requires_manual_review:
                raise ValueError(
                    "requires_manual_review=true is only valid for action='manual_review'."
                )
            if not self.created_tasks:
                raise ValueError(
                    f"action='{self.action}' requires at least one created task."
                )

        return self


class RecoveryDecisionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_run_id: int = Field(..., gt=0)
    source_task_id: int = Field(..., gt=0)
    action: RecoveryAction
    confidence: RecoveryConfidence
    reason: str = Field(..., min_length=10)
    still_blocks_progress: bool
    created_task_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_summary(self) -> "RecoveryDecisionSummary":
        if any(task_id <= 0 for task_id in self.created_task_ids):
            raise ValueError("created_task_ids must contain only positive integers.")
        return self


class RecoveryOpenIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_run_id: int = Field(..., gt=0)
    source_task_id: int = Field(..., gt=0)
    issue_type: str = Field(..., min_length=3)
    summary: str = Field(..., min_length=10)
    recommended_action: str | None = None

    @model_validator(mode="after")
    def normalize_issue(self) -> "RecoveryOpenIssue":
        if self.recommended_action is not None:
            self.recommended_action = self.recommended_action.strip() or None
        return self


class RecoveryCreatedTaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_run_id: int = Field(..., gt=0)
    source_task_id: int = Field(..., gt=0)
    created_task_id: int = Field(..., gt=0)
    title: str = Field(..., min_length=3)
    planning_level: str = Field(..., min_length=3)
    executor_type: str = Field(..., min_length=3)


class RecoveryContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_decisions: list[RecoveryDecisionSummary] = Field(default_factory=list)
    open_issues: list[RecoveryOpenIssue] = Field(default_factory=list)
    recovery_created_tasks: list[RecoveryCreatedTaskRecord] = Field(
        default_factory=list
    )
