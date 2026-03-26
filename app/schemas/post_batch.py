from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.evaluation import StageEvaluationOutput
from app.schemas.recovery import RecoveryContext
from app.schemas.execution_plan import ExecutionPlan


PostBatchStatus = Literal[
    "completed_with_evaluation",
    "checkpoint_blocked",
    "finalization_reopened",
    "finalization_guard_blocked",
    "project_stage_closed",
]


class PostBatchTaskRunSummary(BaseModel):
    task_id: int = Field(..., gt=0)
    run_id: int = Field(..., gt=0)
    run_status: str = Field(..., min_length=3)
    failure_type: str | None = None
    failure_code: str | None = None

    @model_validator(mode="after")
    def normalize_summary(self) -> "PostBatchTaskRunSummary":
        if self.failure_type is not None:
            self.failure_type = self.failure_type.strip() or None
        if self.failure_code is not None:
            self.failure_code = self.failure_code.strip() or None
        return self


class PostBatchResult(BaseModel):
    project_id: int = Field(..., gt=0)
    plan_version: int = Field(..., ge=1)

    batch_id: str = Field(..., min_length=1)
    checkpoint_id: str = Field(..., min_length=1)

    status: PostBatchStatus

    executed_task_ids: list[int] = Field(default_factory=list)
    successful_task_ids: list[int] = Field(default_factory=list)
    problematic_run_ids: list[int] = Field(default_factory=list)

    task_run_summaries: list[PostBatchTaskRunSummary] = Field(default_factory=list)

    recovery_context: RecoveryContext = Field(default_factory=RecoveryContext)
    evaluation_decision: StageEvaluationOutput

    continue_execution: bool
    requires_resequencing: bool
    requires_replanning: bool
    requires_manual_review: bool
    resolved_action: str | None = None
    decision_signals_used: list[str] = Field(default_factory=list)
    patched_execution_plan: ExecutionPlan | None = None

    is_final_batch: bool
    finalization_iteration_count: int = Field(..., ge=0)
    max_finalization_iterations: int = Field(..., ge=0)
    finalization_guard_triggered: bool = False

    notes: str = Field(..., min_length=5)

    @model_validator(mode="after")
    def validate_result(self) -> "PostBatchResult":
        self.notes = self.notes.strip()
        if not self.notes:
            raise ValueError("notes cannot be empty.")

        for collection_name, values in (
            ("executed_task_ids", self.executed_task_ids),
            ("successful_task_ids", self.successful_task_ids),
            ("problematic_run_ids", self.problematic_run_ids),
        ):
            if any(value <= 0 for value in values):
                raise ValueError(f"{collection_name} must contain only positive integers.")

        executed_set = set(self.executed_task_ids)
        successful_set = set(self.successful_task_ids)

        if not successful_set.issubset(executed_set):
            raise ValueError("successful_task_ids must be a subset of executed_task_ids.")

        if self.finalization_iteration_count > self.max_finalization_iterations and not self.finalization_guard_triggered:
            raise ValueError(
                "finalization_iteration_count cannot exceed max_finalization_iterations unless finalization_guard_triggered=true."
            )

        if self.status == "project_stage_closed":
            if self.continue_execution:
                raise ValueError("project_stage_closed status cannot continue execution.")
            if self.requires_replanning or self.requires_resequencing or self.requires_manual_review:
                raise ValueError(
                    "project_stage_closed status cannot require replanning, resequencing, or manual review."
                )
            if not self.evaluation_decision.project_stage_closed:
                raise ValueError(
                    "project_stage_closed status requires evaluation_decision.project_stage_closed=true."
                )

        if self.status == "finalization_reopened":
            if self.continue_execution:
                raise ValueError("finalization_reopened status cannot continue execution immediately.")
            if not (self.requires_replanning or self.requires_resequencing):
                raise ValueError(
                    "finalization_reopened status requires replanning or resequencing."
                )
            if self.evaluation_decision.project_stage_closed:
                raise ValueError(
                    "finalization_reopened status is incompatible with a closed stage."
                )

        if self.status == "finalization_guard_blocked":
            if not self.finalization_guard_triggered:
                raise ValueError(
                    "finalization_guard_blocked status requires finalization_guard_triggered=true."
                )
            if not self.requires_manual_review:
                raise ValueError(
                    "finalization_guard_blocked status requires manual review."
                )
            if self.continue_execution:
                raise ValueError(
                    "finalization_guard_blocked status cannot continue execution."
                )

        if self.status == "completed_with_evaluation":
            if self.evaluation_decision.project_stage_closed:
                raise ValueError(
                    "completed_with_evaluation is incompatible with evaluation_decision.project_stage_closed=true. "
                    "Use project_stage_closed status instead."
                )

        if self.status == "checkpoint_blocked":
            if self.continue_execution:
                raise ValueError("checkpoint_blocked status cannot continue execution.")
            if self.evaluation_decision.project_stage_closed:
                raise ValueError(
                    "checkpoint_blocked is incompatible with a closed stage."
                )

        if self.requires_manual_review and not self.evaluation_decision.manual_review_required:
            raise ValueError(
                "requires_manual_review=true must align with evaluation_decision.manual_review_required=true."
            )

        return self