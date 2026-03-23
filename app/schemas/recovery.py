from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


RecoveryAction = Literal[
    "retry",
    "reatomize",
    "insert_followup",
    "manual_review",
]

RecoveryConfidence = Literal[
    "low",
    "medium",
    "high",
]

RecoveryExecutorType = Literal[
    "code_executor",
]

RecoveryDecisionOrigin = Literal[
    "executor_failure",
    "validator_failure",
    "post_batch_recovery",
    "finalization_recovery",
]


class RecoveryTaskCreate(BaseModel):
    title: str = Field(..., min_length=8)
    description: str = Field(..., min_length=20)
    objective: str | None = None
    implementation_notes: str | None = None
    acceptance_criteria: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None
    task_type: str = Field(default="implementation", min_length=3)
    priority: str = Field(default="medium", min_length=3)
    executor_type: RecoveryExecutorType = "code_executor"

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

        self.title = self.title.strip()
        self.description = self.description.strip()
        self.task_type = self.task_type.strip()
        self.priority = self.priority.strip()

        if not self.title:
            raise ValueError("title cannot be empty.")
        if not self.description:
            raise ValueError("description cannot be empty.")
        if not self.task_type:
            raise ValueError("task_type cannot be empty.")
        if not self.priority:
            raise ValueError("priority cannot be empty.")

        return self


class RecoveryDecision(BaseModel):
    source_task_id: int = Field(..., gt=0)
    source_run_id: int = Field(..., gt=0)

    action: RecoveryAction
    confidence: RecoveryConfidence

    reason: str = Field(..., min_length=10)
    covered_gap_summary: str = Field(..., min_length=10)

    retry_same_task: bool = False
    requires_manual_review: bool = False
    still_blocks_progress: bool = True

    created_tasks: list[RecoveryTaskCreate] = Field(default_factory=list)

    evaluation_guidance: str | None = None
    execution_guidance: str | None = None

    decision_origin: RecoveryDecisionOrigin = "post_batch_recovery"

    @model_validator(mode="after")
    def validate_decision(self) -> "RecoveryDecision":
        self.reason = self.reason.strip()
        self.covered_gap_summary = self.covered_gap_summary.strip()

        if not self.reason:
            raise ValueError("reason cannot be empty.")
        if not self.covered_gap_summary:
            raise ValueError("covered_gap_summary cannot be empty.")

        if self.evaluation_guidance is not None:
            self.evaluation_guidance = self.evaluation_guidance.strip() or None
        if self.execution_guidance is not None:
            self.execution_guidance = self.execution_guidance.strip() or None

        if self.action == "retry":
            if self.created_tasks:
                raise ValueError("created_tasks must be empty when action='retry'.")
            if not self.retry_same_task:
                raise ValueError("retry_same_task must be true when action='retry'.")
            if self.requires_manual_review:
                raise ValueError("action='retry' cannot require manual review.")

        elif self.action == "reatomize":
            if not self.created_tasks:
                raise ValueError("created_tasks must not be empty when action='reatomize'.")
            if self.retry_same_task:
                raise ValueError("retry_same_task must be false when action='reatomize'.")
            if self.requires_manual_review:
                raise ValueError("action='reatomize' cannot require manual review.")

        elif self.action == "insert_followup":
            if not self.created_tasks:
                raise ValueError("created_tasks must not be empty when action='insert_followup'.")
            if self.retry_same_task:
                raise ValueError("retry_same_task must be false when action='insert_followup'.")
            if self.requires_manual_review:
                raise ValueError("action='insert_followup' cannot require manual review.")

        elif self.action == "manual_review":
            if self.created_tasks:
                raise ValueError("created_tasks must be empty when action='manual_review'.")
            if self.retry_same_task:
                raise ValueError("retry_same_task must be false when action='manual_review'.")
            if not self.requires_manual_review:
                raise ValueError("requires_manual_review must be true when action='manual_review'.")
            if not self.still_blocks_progress:
                raise ValueError("action='manual_review' must keep still_blocks_progress=true.")

        return self


class RecoveryCreatedTaskRecord(BaseModel):
    source_task_id: int = Field(..., gt=0)
    source_run_id: int = Field(..., gt=0)
    created_task_id: int = Field(..., gt=0)
    title: str = Field(..., min_length=3)
    planning_level: str = Field(..., min_length=3)
    executor_type: str = Field(..., min_length=3)


class RecoveryDecisionSummary(BaseModel):
    source_task_id: int = Field(..., gt=0)
    source_run_id: int = Field(..., gt=0)
    action: RecoveryAction
    confidence: RecoveryConfidence
    reason: str = Field(..., min_length=10)
    still_blocks_progress: bool
    created_task_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_summary(self) -> "RecoveryDecisionSummary":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("reason cannot be empty.")

        if any(task_id <= 0 for task_id in self.created_task_ids):
            raise ValueError("created_task_ids must contain only positive integers.")

        return self


class RecoveryOpenIssue(BaseModel):
    source_task_id: int = Field(..., gt=0)
    source_run_id: int = Field(..., gt=0)
    issue_type: str = Field(..., min_length=3)
    summary: str = Field(..., min_length=10)
    recommended_action: str | None = None

    @model_validator(mode="after")
    def normalize_issue(self) -> "RecoveryOpenIssue":
        self.issue_type = self.issue_type.strip()
        self.summary = self.summary.strip()

        if not self.issue_type:
            raise ValueError("issue_type cannot be empty.")
        if not self.summary:
            raise ValueError("summary cannot be empty.")

        if self.recommended_action is not None:
            self.recommended_action = self.recommended_action.strip() or None

        return self


class RecoveryContext(BaseModel):
    recovery_decisions: list[RecoveryDecisionSummary] = Field(default_factory=list)
    open_issues: list[RecoveryOpenIssue] = Field(default_factory=list)
    recovery_created_tasks: list[RecoveryCreatedTaskRecord] = Field(default_factory=list)