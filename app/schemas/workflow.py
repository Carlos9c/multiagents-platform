from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.schemas.post_batch_intent import (
    ResolvedPostBatchIntentType,
    ResolvedPostBatchMutationScope,
)


class WorkflowIterationSummary(BaseModel):
    iteration_number: int = Field(..., ge=1)

    plan_version: int = Field(..., ge=1)
    starting_plan_version: int = Field(..., ge=1)
    ending_plan_version: int = Field(..., ge=1)

    batch_ids_processed: list[str] = Field(default_factory=list)
    blocked_batch_ids_after_iteration: list[str] = Field(default_factory=list)

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

    used_patched_plan: bool = False

    decision_signals: list[str] = Field(default_factory=list)
    notes: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_summary(self) -> "WorkflowIterationSummary":
        self.notes = self.notes.strip()
        if not self.notes:
            raise ValueError("notes cannot be empty.")

        normalized_processed: list[str] = []
        seen_processed: set[str] = set()
        for batch_id in self.batch_ids_processed:
            normalized = batch_id.strip()
            if not normalized or normalized in seen_processed:
                continue
            normalized_processed.append(normalized)
            seen_processed.add(normalized)
        self.batch_ids_processed = normalized_processed

        normalized_blocked: list[str] = []
        seen_blocked: set[str] = set()
        for batch_id in self.blocked_batch_ids_after_iteration:
            normalized = batch_id.strip()
            if not normalized or normalized in seen_blocked:
                continue
            normalized_blocked.append(normalized)
            seen_blocked.add(normalized)
        self.blocked_batch_ids_after_iteration = normalized_blocked

        normalized_signals: list[str] = []
        seen_signals: set[str] = set()
        for signal in self.decision_signals:
            normalized = signal.strip()
            if not normalized or normalized in seen_signals:
                continue
            normalized_signals.append(normalized)
            seen_signals.add(normalized)
        self.decision_signals = normalized_signals

        if self.starting_plan_version > self.ending_plan_version:
            raise ValueError(
                "starting_plan_version cannot be greater than ending_plan_version."
            )

        if not (self.starting_plan_version <= self.plan_version <= self.ending_plan_version):
            raise ValueError(
                "plan_version must be within [starting_plan_version, ending_plan_version]."
            )

        processed_set = set(self.batch_ids_processed)
        blocked_set = set(self.blocked_batch_ids_after_iteration)
        if processed_set & blocked_set:
            raise ValueError(
                "batch_ids_processed and blocked_batch_ids_after_iteration must be disjoint."
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
            raise ValueError(
                "should_close_stage=True requires resolved_intent_type='close'."
            )

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

        if self.should_close_stage and self.blocked_batch_ids_after_iteration:
            raise ValueError(
                "should_close_stage=True is incompatible with blocked batches remaining after iteration."
            )

        return self


class ProjectWorkflowResult(BaseModel):
    project_id: int = Field(..., gt=0)
    status: str = Field(..., min_length=1)

    planning_completed: bool
    refinement_completed: bool
    atomic_generation_completed: bool
    execution_plan_generated: bool

    plan_version: int | None = Field(default=None, ge=1)

    completed_batches: list[str] = Field(default_factory=list)
    blocked_batches: list[str] = Field(default_factory=list)
    iterations: list[WorkflowIterationSummary] = Field(default_factory=list)

    manual_review_required: bool = False
    final_stage_closed: bool = False
    notes: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "ProjectWorkflowResult":
        self.status = self.status.strip()
        if not self.status:
            raise ValueError("status cannot be empty.")

        if self.notes is not None:
            self.notes = self.notes.strip() or None

        normalized_completed: list[str] = []
        seen_completed: set[str] = set()
        for batch_id in self.completed_batches:
            normalized = batch_id.strip()
            if not normalized or normalized in seen_completed:
                continue
            normalized_completed.append(normalized)
            seen_completed.add(normalized)
        self.completed_batches = normalized_completed

        normalized_blocked: list[str] = []
        seen_blocked: set[str] = set()
        for batch_id in self.blocked_batches:
            normalized = batch_id.strip()
            if not normalized or normalized in seen_blocked:
                continue
            normalized_blocked.append(normalized)
            seen_blocked.add(normalized)
        self.blocked_batches = normalized_blocked

        completed_set = set(self.completed_batches)
        blocked_set = set(self.blocked_batches)
        if completed_set & blocked_set:
            raise ValueError(
                "completed_batches and blocked_batches must be disjoint."
            )

        if self.final_stage_closed and self.manual_review_required:
            raise ValueError(
                "final_stage_closed=True is incompatible with manual_review_required=True."
            )

        if self.final_stage_closed and self.status != "stage_closed":
            raise ValueError(
                "final_stage_closed=True requires status='stage_closed'."
            )

        if self.manual_review_required and self.status != "awaiting_manual_review":
            raise ValueError(
                "manual_review_required=True requires status='awaiting_manual_review'."
            )

        if self.status == "stage_closed" and not self.final_stage_closed:
            raise ValueError(
                "status='stage_closed' requires final_stage_closed=True."
            )

        if self.status == "awaiting_manual_review" and not self.manual_review_required:
            raise ValueError(
                "status='awaiting_manual_review' requires manual_review_required=True."
            )

        if self.iterations:
            last_iteration = self.iterations[-1]

            if self.plan_version is not None and self.plan_version != last_iteration.plan_version:
                raise ValueError(
                    "plan_version must match the last iteration plan_version."
                )

            if self.final_stage_closed and not last_iteration.should_close_stage:
                raise ValueError(
                    "final_stage_closed=True requires the last iteration to have should_close_stage=True."
                )

            if self.manual_review_required and not last_iteration.requires_manual_review:
                raise ValueError(
                    "manual_review_required=True requires the last iteration to have requires_manual_review=True."
                )

        return self