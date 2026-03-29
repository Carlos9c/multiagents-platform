from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.evaluation import StageEvaluationOutput
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.post_batch_intent import (
    ResolvedPostBatchIntentType,
    ResolvedPostBatchMutationScope,
)
from app.schemas.recovery import RecoveryContext

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
        self.run_status = self.run_status.strip()
        if len(self.run_status) < 3:
            raise ValueError("run_status cannot be empty or shorter than 3 characters.")

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

    resolved_intent_type: ResolvedPostBatchIntentType
    resolved_mutation_scope: ResolvedPostBatchMutationScope

    remaining_plan_still_valid: bool
    has_new_recovery_tasks: bool
    requires_plan_mutation: bool
    requires_all_new_tasks_assigned: bool

    can_continue_after_application: bool
    should_close_stage: bool
    requires_manual_review: bool
    reopened_finalization: bool

    decision_signals: list[str] = Field(default_factory=list)
    patched_execution_plan: ExecutionPlan | None = None

    is_final_batch: bool
    finalization_iteration_count: int = Field(..., ge=0)
    max_finalization_iterations: int = Field(..., ge=0)
    finalization_guard_triggered: bool = False

    notes: str = Field(..., min_length=5)

    @model_validator(mode="after")
    def validate_result(self) -> "PostBatchResult":
        self.batch_id = self.batch_id.strip()
        self.checkpoint_id = self.checkpoint_id.strip()
        self.notes = self.notes.strip()

        if not self.batch_id:
            raise ValueError("batch_id cannot be empty.")
        if not self.checkpoint_id:
            raise ValueError("checkpoint_id cannot be empty.")
        if not self.notes:
            raise ValueError("notes cannot be empty.")

        normalized_signals: list[str] = []
        seen_signals: set[str] = set()
        for item in self.decision_signals:
            normalized = item.strip()
            if not normalized or normalized in seen_signals:
                continue
            normalized_signals.append(normalized)
            seen_signals.add(normalized)
        self.decision_signals = normalized_signals

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

        if (
            self.finalization_iteration_count > self.max_finalization_iterations
            and not self.finalization_guard_triggered
        ):
            raise ValueError(
                "finalization_iteration_count cannot exceed max_finalization_iterations "
                "unless finalization_guard_triggered=true."
            )

        if self.resolved_mutation_scope == "none" and self.requires_plan_mutation:
            raise ValueError(
                "resolved_mutation_scope='none' is incompatible with requires_plan_mutation=True."
            )

        if self.resolved_mutation_scope != "none" and not self.requires_plan_mutation:
            raise ValueError(
                "resolved_mutation_scope!='none' requires requires_plan_mutation=True."
            )

        if self.requires_all_new_tasks_assigned and not self.has_new_recovery_tasks:
            raise ValueError(
                "requires_all_new_tasks_assigned=True requires has_new_recovery_tasks=True."
            )

        if self.should_close_stage and self.resolved_intent_type != "close":
            raise ValueError("should_close_stage=True requires resolved_intent_type='close'.")

        if self.requires_manual_review and self.resolved_intent_type != "manual_review":
            raise ValueError(
                "requires_manual_review=True requires resolved_intent_type='manual_review'."
            )

        if self.reopened_finalization and self.resolved_intent_type in {
            "continue",
            "manual_review",
            "close",
        }:
            raise ValueError(
                "reopened_finalization=True is incompatible with resolved_intent_type "
                "in {'continue', 'manual_review', 'close'}."
            )

        if self.resolved_intent_type == "continue":
            if self.resolved_mutation_scope != "none":
                raise ValueError(
                    "resolved_intent_type='continue' requires resolved_mutation_scope='none'."
                )
            if self.requires_plan_mutation:
                raise ValueError("resolved_intent_type='continue' cannot require plan mutation.")
            if self.has_new_recovery_tasks:
                raise ValueError("resolved_intent_type='continue' cannot carry new recovery tasks.")
            if self.requires_all_new_tasks_assigned:
                raise ValueError(
                    "resolved_intent_type='continue' cannot require new task assignment."
                )
            if self.requires_manual_review:
                raise ValueError("resolved_intent_type='continue' cannot require manual review.")
            if self.should_close_stage:
                raise ValueError("resolved_intent_type='continue' cannot close the stage.")
            if self.reopened_finalization:
                raise ValueError("resolved_intent_type='continue' cannot reopen finalization.")
            if not self.can_continue_after_application:
                raise ValueError("resolved_intent_type='continue' must allow continuation.")
            if self.patched_execution_plan is not None:
                raise ValueError(
                    "resolved_intent_type='continue' cannot include a patched_execution_plan."
                )

        elif self.resolved_intent_type == "assign":
            if self.resolved_mutation_scope != "assignment":
                raise ValueError(
                    "resolved_intent_type='assign' requires resolved_mutation_scope='assignment'."
                )
            if not self.requires_plan_mutation:
                raise ValueError("resolved_intent_type='assign' must require plan mutation.")
            if not self.has_new_recovery_tasks:
                raise ValueError(
                    "resolved_intent_type='assign' requires has_new_recovery_tasks=True."
                )
            if not self.requires_all_new_tasks_assigned:
                raise ValueError(
                    "resolved_intent_type='assign' must require all new tasks to be assigned."
                )
            if self.requires_manual_review:
                raise ValueError("resolved_intent_type='assign' cannot require manual review.")
            if self.should_close_stage:
                raise ValueError("resolved_intent_type='assign' cannot close the stage.")
            if not self.can_continue_after_application:
                raise ValueError(
                    "resolved_intent_type='assign' must allow continuation after application."
                )
            if self.patched_execution_plan is None:
                raise ValueError("resolved_intent_type='assign' requires a patched_execution_plan.")

        elif self.resolved_intent_type == "resequence":
            if self.resolved_mutation_scope != "resequence":
                raise ValueError(
                    "resolved_intent_type='resequence' requires resolved_mutation_scope='resequence'."
                )
            if not self.requires_plan_mutation:
                raise ValueError("resolved_intent_type='resequence' must require plan mutation.")
            if self.requires_manual_review:
                raise ValueError("resolved_intent_type='resequence' cannot require manual review.")
            if self.should_close_stage:
                raise ValueError("resolved_intent_type='resequence' cannot close the stage.")
            if self.can_continue_after_application:
                raise ValueError(
                    "resolved_intent_type='resequence' must not continue before applying the resequenced plan."
                )

        elif self.resolved_intent_type == "replan":
            if self.resolved_mutation_scope != "replan":
                raise ValueError(
                    "resolved_intent_type='replan' requires resolved_mutation_scope='replan'."
                )
            if not self.requires_plan_mutation:
                raise ValueError("resolved_intent_type='replan' must require plan mutation.")
            if self.remaining_plan_still_valid:
                raise ValueError(
                    "resolved_intent_type='replan' requires remaining_plan_still_valid=False."
                )
            if self.requires_manual_review:
                raise ValueError("resolved_intent_type='replan' cannot require manual review.")
            if self.should_close_stage:
                raise ValueError("resolved_intent_type='replan' cannot close the stage.")
            if self.can_continue_after_application:
                raise ValueError(
                    "resolved_intent_type='replan' must not continue before generating the new plan."
                )

        elif self.resolved_intent_type == "manual_review":
            if self.resolved_mutation_scope != "none":
                raise ValueError(
                    "resolved_intent_type='manual_review' requires resolved_mutation_scope='none'."
                )
            if self.requires_plan_mutation:
                raise ValueError(
                    "resolved_intent_type='manual_review' cannot require plan mutation."
                )
            if self.requires_all_new_tasks_assigned:
                raise ValueError(
                    "resolved_intent_type='manual_review' cannot require automatic assignment."
                )
            if not self.requires_manual_review:
                raise ValueError(
                    "resolved_intent_type='manual_review' requires requires_manual_review=True."
                )
            if self.can_continue_after_application:
                raise ValueError(
                    "resolved_intent_type='manual_review' cannot continue automatically."
                )
            if self.should_close_stage:
                raise ValueError("resolved_intent_type='manual_review' cannot close the stage.")
            if self.reopened_finalization:
                raise ValueError(
                    "resolved_intent_type='manual_review' cannot reopen finalization by itself."
                )
            if self.patched_execution_plan is not None:
                raise ValueError(
                    "resolved_intent_type='manual_review' cannot include a patched_execution_plan."
                )

        elif self.resolved_intent_type == "close":
            if self.resolved_mutation_scope != "none":
                raise ValueError(
                    "resolved_intent_type='close' requires resolved_mutation_scope='none'."
                )
            if self.requires_plan_mutation:
                raise ValueError("resolved_intent_type='close' cannot require plan mutation.")
            if self.requires_all_new_tasks_assigned:
                raise ValueError(
                    "resolved_intent_type='close' cannot require assignment of new work."
                )
            if self.has_new_recovery_tasks:
                raise ValueError(
                    "resolved_intent_type='close' is invalid while new recovery tasks still exist."
                )
            if not self.should_close_stage:
                raise ValueError("resolved_intent_type='close' requires should_close_stage=True.")
            if self.requires_manual_review:
                raise ValueError("resolved_intent_type='close' cannot require manual review.")
            if self.can_continue_after_application:
                raise ValueError("resolved_intent_type='close' cannot continue execution.")
            if self.reopened_finalization:
                raise ValueError("resolved_intent_type='close' cannot reopen finalization.")
            if self.patched_execution_plan is not None:
                raise ValueError(
                    "resolved_intent_type='close' cannot include a patched_execution_plan."
                )

        else:
            raise ValueError(f"Unsupported resolved_intent_type: {self.resolved_intent_type}")

        if self.status == "project_stage_closed":
            if self.resolved_intent_type != "close":
                raise ValueError(
                    "status='project_stage_closed' requires resolved_intent_type='close'."
                )
            if not self.should_close_stage:
                raise ValueError("status='project_stage_closed' requires should_close_stage=True.")
            if not self.evaluation_decision.project_stage_closed:
                raise ValueError(
                    "status='project_stage_closed' requires "
                    "evaluation_decision.project_stage_closed=true."
                )

        if self.status == "finalization_reopened":
            if not self.reopened_finalization:
                raise ValueError(
                    "status='finalization_reopened' requires reopened_finalization=True."
                )
            if self.resolved_intent_type not in {"assign", "resequence", "replan"}:
                raise ValueError(
                    "status='finalization_reopened' requires an intent that mutates the remaining plan."
                )
            if self.evaluation_decision.project_stage_closed:
                raise ValueError(
                    "status='finalization_reopened' is incompatible with a closed stage."
                )

        if self.status == "finalization_guard_blocked":
            if not self.finalization_guard_triggered:
                raise ValueError(
                    "status='finalization_guard_blocked' requires finalization_guard_triggered=true."
                )
            if self.resolved_intent_type != "manual_review":
                raise ValueError(
                    "status='finalization_guard_blocked' requires resolved_intent_type='manual_review'."
                )
            if not self.requires_manual_review:
                raise ValueError(
                    "status='finalization_guard_blocked' requires requires_manual_review=True."
                )
            if self.can_continue_after_application:
                raise ValueError("status='finalization_guard_blocked' cannot continue execution.")

        if self.status == "completed_with_evaluation":
            if self.evaluation_decision.project_stage_closed:
                raise ValueError(
                    "status='completed_with_evaluation' is incompatible with "
                    "evaluation_decision.project_stage_closed=true. "
                    "Use 'project_stage_closed' instead."
                )
            if self.reopened_finalization:
                raise ValueError(
                    "status='completed_with_evaluation' cannot mark reopened_finalization=True."
                )

        if self.status == "checkpoint_blocked":
            if self.resolved_intent_type == "continue":
                raise ValueError(
                    "status='checkpoint_blocked' cannot use resolved_intent_type='continue'."
                )
            if self.can_continue_after_application and self.resolved_intent_type != "assign":
                raise ValueError(
                    "status='checkpoint_blocked' only allows can_continue_after_application=True for assign."
                )
            if self.evaluation_decision.project_stage_closed:
                raise ValueError("status='checkpoint_blocked' is incompatible with a closed stage.")

        if (
            self.requires_manual_review
            and not self.evaluation_decision.manual_review_required
            and not self.finalization_guard_triggered
        ):
            raise ValueError(
                "requires_manual_review=true must align with "
                "evaluation_decision.manual_review_required=true unless manual review "
                "was triggered by the finalization guard."
            )

        if self.should_close_stage and not self.evaluation_decision.project_stage_closed:
            raise ValueError(
                "should_close_stage=true requires evaluation_decision.project_stage_closed=true."
            )

        if self.resolved_intent_type == "assign" and self.patched_execution_plan is None:
            raise ValueError(
                "resolved_intent_type='assign' requires patched_execution_plan to be present."
            )

        if not self.requires_plan_mutation and self.patched_execution_plan is not None:
            raise ValueError(
                "patched_execution_plan is only valid when requires_plan_mutation=true."
            )

        return self
