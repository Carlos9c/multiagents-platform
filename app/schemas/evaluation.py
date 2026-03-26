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
    "reatomize_failed_tasks",
    "insert_followup_atomic_tasks",
    "replan_from_high_level",
    "manual_review",
]

RecommendedNextAction = Literal[
    "close_stage",
    "continue_current_plan",
    "resequence_remaining_batches",
    "replan_remaining_work",
    "manual_review",
]

PlanChangeScope = Literal[
    "none",
    "local_resequencing",
    "remaining_plan_rebuild",
    "high_level_replan",
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

        if not self.required:
            self.level = None
            self.reason = None
            self.target_task_ids = []

        return self


class StageEvaluationOutput(BaseModel):
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

    recommended_next_action: RecommendedNextAction | None = None
    recommended_next_action_reason: str | None = None

    # Nuevas señales estructuradas de razonamiento operativo
    decision_signals: list[str] = Field(default_factory=list)
    plan_change_scope: PlanChangeScope = "none"
    remaining_plan_still_valid: bool = True
    new_recovery_tasks_blocking: bool | None = None
    single_task_tail_risk: bool = False

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

        if self.recommended_next_action_reason is not None:
            self.recommended_next_action_reason = self.recommended_next_action_reason.strip() or None

        self.decision_signals = [
            item.strip() for item in self.decision_signals if item and item.strip()
        ]
        self.key_risks = [item.strip() for item in self.key_risks if item and item.strip()]
        self.notes = [item.strip() for item in self.notes if item and item.strip()]

        if self.followup_atomic_tasks_required and not self.followup_atomic_tasks_reason:
            raise ValueError(
                "followup_atomic_tasks_reason is required when followup_atomic_tasks_required is true."
            )

        if self.manual_review_required and not self.manual_review_reason:
            raise ValueError("manual_review_reason is required when manual_review_required is true.")

        if self.recovery_strategy != "none" and not self.recovery_reason:
            raise ValueError("recovery_reason is required when recovery_strategy is not 'none'.")

        if self.recovery_strategy == "insert_followup_atomic_tasks":
            if not self.followup_atomic_tasks_required:
                raise ValueError(
                    "recovery_strategy='insert_followup_atomic_tasks' requires "
                    "followup_atomic_tasks_required=true."
                )

        if self.recovery_strategy == "manual_review":
            if not self.manual_review_required:
                raise ValueError(
                    "recovery_strategy='manual_review' requires manual_review_required=true."
                )

        if self.recovery_strategy == "replan_from_high_level":
            if not self.replan.required or self.replan.level != "high_level":
                raise ValueError(
                    "recovery_strategy='replan_from_high_level' requires replan.required=true "
                    "and replan.level='high_level'."
                )

        if self.recovery_strategy == "reatomize_failed_tasks":
            if self.replan.required and self.replan.level == "high_level":
                raise ValueError(
                    "recovery_strategy='reatomize_failed_tasks' cannot coexist with "
                    "replan.level='high_level'."
                )

        if self.replan.required and self.replan.level == "high_level":
            if self.remaining_plan_still_valid:
                raise ValueError(
                    "remaining_plan_still_valid must be false when high-level replanning is required."
                )
            if self.plan_change_scope != "high_level_replan":
                raise ValueError(
                    "plan_change_scope must be 'high_level_replan' when high-level replanning is required."
                )

        if self.decision == "stage_completed":
            if not self.project_stage_closed:
                raise ValueError(
                    "project_stage_closed must be true when decision is 'stage_completed'."
                )
            if self.manual_review_required:
                raise ValueError("stage_completed cannot require manual review.")
            if self.recovery_strategy != "none":
                raise ValueError(
                    "stage_completed must not request a recovery strategy other than 'none'."
                )
            if self.replan.required:
                raise ValueError("stage_completed must not request replanning.")
            if self.followup_atomic_tasks_required:
                raise ValueError("stage_completed must not request follow-up atomic tasks.")
            if self.recommended_next_action not in {None, "close_stage"}:
                raise ValueError(
                    "stage_completed only allows recommended_next_action='close_stage'."
                )
            if self.plan_change_scope != "none":
                raise ValueError("stage_completed requires plan_change_scope='none'.")
            if not self.remaining_plan_still_valid:
                raise ValueError(
                    "stage_completed requires remaining_plan_still_valid=true."
                )

        if self.decision == "manual_review_required":
            if not self.manual_review_required:
                raise ValueError(
                    "decision='manual_review_required' requires manual_review_required=true."
                )
            if self.recommended_next_action not in {None, "manual_review"}:
                raise ValueError(
                    "decision='manual_review_required' only allows "
                    "recommended_next_action='manual_review'."
                )

        if self.decision == "stage_incomplete":
            if self.project_stage_closed:
                raise ValueError(
                    "decision='stage_incomplete' cannot set project_stage_closed=true."
                )

        if self.recommended_next_action is not None and not self.recommended_next_action_reason:
            raise ValueError(
                "recommended_next_action_reason is required when recommended_next_action is set."
            )

        if self.recommended_next_action == "close_stage":
            if self.decision != "stage_completed" or not self.project_stage_closed:
                raise ValueError(
                    "recommended_next_action='close_stage' requires a closed stage."
                )
            if self.plan_change_scope != "none":
                raise ValueError(
                    "recommended_next_action='close_stage' requires plan_change_scope='none'."
                )

        if self.recommended_next_action == "manual_review":
            if not self.manual_review_required:
                raise ValueError(
                    "recommended_next_action='manual_review' requires "
                    "manual_review_required=true."
                )

        if self.recommended_next_action == "continue_current_plan":
            if self.project_stage_closed:
                raise ValueError(
                    "recommended_next_action='continue_current_plan' is incompatible with "
                    "project_stage_closed=true."
                )
            if self.manual_review_required:
                raise ValueError(
                    "recommended_next_action='continue_current_plan' is incompatible with "
                    "manual_review_required=true."
                )
            if self.replan.required:
                raise ValueError(
                    "recommended_next_action='continue_current_plan' is incompatible with "
                    "replan.required=true."
                )
            if self.followup_atomic_tasks_required:
                raise ValueError(
                    "recommended_next_action='continue_current_plan' is incompatible with "
                    "followup_atomic_tasks_required=true."
                )
            if self.plan_change_scope != "none":
                raise ValueError(
                    "recommended_next_action='continue_current_plan' requires "
                    "plan_change_scope='none'."
                )
            if not self.remaining_plan_still_valid:
                raise ValueError(
                    "recommended_next_action='continue_current_plan' requires "
                    "remaining_plan_still_valid=true."
                )

        if self.recommended_next_action == "resequence_remaining_batches":
            if self.project_stage_closed:
                raise ValueError(
                    "recommended_next_action='resequence_remaining_batches' is incompatible with "
                    "project_stage_closed=true."
                )
            if self.manual_review_required:
                raise ValueError(
                    "recommended_next_action='resequence_remaining_batches' is incompatible with "
                    "manual_review_required=true."
                )
            if self.replan.required and self.replan.level == "high_level":
                raise ValueError(
                    "recommended_next_action='resequence_remaining_batches' cannot coexist with "
                    "high-level replanning."
                )
            if self.plan_change_scope not in {
                "local_resequencing",
                "remaining_plan_rebuild",
            }:
                raise ValueError(
                    "recommended_next_action='resequence_remaining_batches' requires "
                    "plan_change_scope in {'local_resequencing', 'remaining_plan_rebuild'}."
                )
            if not self.remaining_plan_still_valid:
                raise ValueError(
                    "recommended_next_action='resequence_remaining_batches' requires "
                    "remaining_plan_still_valid=true."
                )
            if (
                not self.followup_atomic_tasks_required
                and self.recovery_strategy not in {
                    "reatomize_failed_tasks",
                    "insert_followup_atomic_tasks",
                }
                and not (self.replan.required and self.replan.level == "atomic")
            ):
                raise ValueError(
                    "recommended_next_action='resequence_remaining_batches' requires a local "
                    "recovery/resequencing signal."
                )

        if self.recommended_next_action == "replan_remaining_work":
            if not self.replan.required or self.replan.level != "high_level":
                raise ValueError(
                    "recommended_next_action='replan_remaining_work' requires "
                    "replan.required=true and replan.level='high_level'."
                )
            if self.project_stage_closed:
                raise ValueError(
                    "recommended_next_action='replan_remaining_work' is incompatible with "
                    "project_stage_closed=true."
                )
            if self.manual_review_required:
                raise ValueError(
                    "recommended_next_action='replan_remaining_work' is incompatible with "
                    "manual_review_required=true."
                )
            if self.plan_change_scope != "high_level_replan":
                raise ValueError(
                    "recommended_next_action='replan_remaining_work' requires "
                    "plan_change_scope='high_level_replan'."
                )
            if self.remaining_plan_still_valid:
                raise ValueError(
                    "recommended_next_action='replan_remaining_work' requires "
                    "remaining_plan_still_valid=false."
                )

        return self