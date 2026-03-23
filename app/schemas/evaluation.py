from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator



StageEvaluationDecision = Literal[
    "stage_completed",
    "stage_incomplete",
    "manual_review_required",
]

ReplanLevel = Literal[
    "atomic",
    "high_level",
]

BatchOutcome = Literal[
    "successful",
    "partial",
    "failed",
    "blocked",
]

RecoveryStrategy = Literal[
    "none",
    "retry_batch",
    "reatomize_failed_tasks",
    "insert_followup_atomic_tasks",
    "replan_from_high_level",
    "manual_review",
]


class EvaluatedBatchSummary(BaseModel):
    batch_id: str = Field(..., min_length=1)
    outcome: BatchOutcome
    summary: str = Field(..., min_length=10)
    key_findings: list[str] = Field(default_factory=list)
    failed_task_ids: list[int] = Field(default_factory=list)
    partial_task_ids: list[int] = Field(default_factory=list)
    completed_task_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_batch_summary(self) -> "EvaluatedBatchSummary":
        self.key_findings = [item.strip() for item in self.key_findings if item and item.strip()]

        for collection_name, values in (
            ("failed_task_ids", self.failed_task_ids),
            ("partial_task_ids", self.partial_task_ids),
            ("completed_task_ids", self.completed_task_ids),
        ):
            if any(task_id <= 0 for task_id in values):
                raise ValueError(f"{collection_name} must contain only positive integers.")

        return self


class EvaluationReplanInstruction(BaseModel):
    """
    Replanning contract aligned with the active workflow.

    refined is intentionally unsupported.
    """

    required: bool
    level: ReplanLevel | None = None
    reason: str | None = None
    target_task_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_instruction(self) -> "EvaluationReplanInstruction":
        if self.reason is not None:
            self.reason = self.reason.strip() or None

        if any(task_id <= 0 for task_id in self.target_task_ids):
            raise ValueError("target_task_ids must contain only positive integers.")

        if self.required and self.level is None:
            raise ValueError("level is required when replanning is required.")

        if self.required and not self.reason:
            raise ValueError("reason is required when replanning is required.")

        return self


class RecoveryCreatedTask(BaseModel):
    source_run_id: int = Field(..., gt=0)
    source_task_id: int = Field(..., gt=0)
    created_task_id: int = Field(..., gt=0)
    title: str = Field(..., min_length=3)
    planning_level: str = Field(..., min_length=3)
    executor_type: str = Field(..., min_length=3)


class RecoveryIssue(BaseModel):
    source_run_id: int = Field(..., gt=0)
    source_task_id: int = Field(..., gt=0)
    issue_type: str = Field(..., min_length=3)
    summary: str = Field(..., min_length=10)
    recommended_action: str | None = None

    @model_validator(mode="after")
    def normalize_issue(self) -> "RecoveryIssue":
        if self.recommended_action is not None:
            self.recommended_action = self.recommended_action.strip() or None
        return self


class RecoveryDecisionSummary(BaseModel):
    source_run_id: int = Field(..., gt=0)
    source_task_id: int = Field(..., gt=0)
    strategy: str = Field(..., min_length=3)
    reason: str = Field(..., min_length=10)
    created_task_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_summary(self) -> "RecoveryDecisionSummary":
        if any(task_id <= 0 for task_id in self.created_task_ids):
            raise ValueError("created_task_ids must contain only positive integers.")
        return self


class StageEvaluationOutput(BaseModel):
    """
    Final evaluator output consumed by post-batch / recovery orchestration.
    """

    decision: StageEvaluationDecision
    decision_summary: str = Field(..., min_length=20)

    stage_goals_satisfied: bool
    project_stage_closed: bool

    recovery_strategy: RecoveryStrategy = "none"
    recovery_reason: str | None = None

    replan: EvaluationReplanInstruction = Field(
        default_factory=lambda: EvaluationReplanInstruction(required=False)
    )

    followup_atomic_tasks_required: bool = False
    followup_atomic_tasks_reason: str | None = None

    manual_review_required: bool = False
    manual_review_reason: str | None = None

    evaluated_batches: list[EvaluatedBatchSummary] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_output(self) -> "StageEvaluationOutput":
        self.decision_summary = self.decision_summary.strip()
        if not self.decision_summary:
            raise ValueError("decision_summary cannot be empty.")

        if self.recovery_reason is not None:
            self.recovery_reason = self.recovery_reason.strip() or None

        if self.followup_atomic_tasks_reason is not None:
            self.followup_atomic_tasks_reason = self.followup_atomic_tasks_reason.strip() or None

        if self.manual_review_reason is not None:
            self.manual_review_reason = self.manual_review_reason.strip() or None

        self.key_risks = [item.strip() for item in self.key_risks if item and item.strip()]
        self.notes = [item.strip() for item in self.notes if item and item.strip()]

        if self.decision == "stage_completed":
            if not self.project_stage_closed:
                raise ValueError("project_stage_closed must be true when decision is 'stage_completed'.")
            if self.manual_review_required:
                raise ValueError("stage_completed cannot require manual review.")
            if self.recovery_strategy != "none":
                raise ValueError("stage_completed must not request a recovery strategy other than 'none'.")
            if self.replan.required:
                raise ValueError("stage_completed must not request replanning.")
            if self.followup_atomic_tasks_required:
                raise ValueError("stage_completed must not request follow-up atomic tasks.")

        if self.decision == "manual_review_required" and not self.manual_review_required:
            raise ValueError(
                "decision='manual_review_required' requires manual_review_required=true."
            )

        if self.manual_review_required and not self.manual_review_reason:
            raise ValueError("manual_review_reason is required when manual_review_required is true.")

        if self.followup_atomic_tasks_required and not self.followup_atomic_tasks_reason:
            raise ValueError(
                "followup_atomic_tasks_reason is required when followup_atomic_tasks_required is true."
            )

        if self.recovery_strategy != "none" and not self.recovery_reason:
            raise ValueError("recovery_reason is required when recovery_strategy is not 'none'.")

        if self.recovery_strategy == "insert_followup_atomic_tasks":
            if not self.followup_atomic_tasks_required:
                raise ValueError(
                    "recovery_strategy='insert_followup_atomic_tasks' requires followup_atomic_tasks_required=true."
                )

        if self.recovery_strategy == "manual_review":
            if not self.manual_review_required:
                raise ValueError(
                    "recovery_strategy='manual_review' requires manual_review_required=true."
                )

        if self.recovery_strategy == "replan_from_high_level":
            if not self.replan.required or self.replan.level != "high_level":
                raise ValueError(
                    "recovery_strategy='replan_from_high_level' requires replan.required=true and replan.level='high_level'."
                )

        if self.recovery_strategy == "reatomize_failed_tasks":
            if self.replan.required and self.replan.level == "high_level":
                raise ValueError(
                    "recovery_strategy='reatomize_failed_tasks' cannot coexist with replan.level='high_level'."
                )

        return self