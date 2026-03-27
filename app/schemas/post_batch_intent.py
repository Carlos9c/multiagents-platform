from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ResolvedPostBatchIntentType = Literal[
    "continue",
    "assign",
    "resequence",
    "replan",
    "manual_review",
    "close",
]

ResolvedPostBatchMutationScope = Literal[
    "none",
    "assignment",
    "resequence",
    "replan",
]


LegacyResolvedPostBatchAction = Literal[
    "continue_current_plan",
    "resequence_remaining_batches",
    "replan_remaining_work",
    "manual_review",
    "close_stage",
]


@dataclass(frozen=True, kw_only=True)
class ResolvedPostBatchIntent:
    """
    Canonical post-batch intent.

    This object is the single resolved interpretation of:
    - evaluator signals
    - recovery side-effects
    - pending work state
    - end-of-plan semantics

    It should be the only contract consumed by post_batch_service for deciding
    what to do next.
    """

    intent_type: ResolvedPostBatchIntentType
    legacy_action: LegacyResolvedPostBatchAction
    mutation_scope: ResolvedPostBatchMutationScope

    remaining_plan_still_valid: bool
    has_new_recovery_tasks: bool
    requires_plan_mutation: bool
    requires_all_new_tasks_assigned: bool

    can_continue_after_application: bool
    should_close_stage: bool
    requires_manual_review: bool
    reopened_finalization: bool

    notes: str
    decision_signals: list[str]

    def __post_init__(self) -> None:
        if not self.notes.strip():
            raise ValueError("ResolvedPostBatchIntent.notes cannot be empty.")

        if self.intent_type == "continue":
            if self.legacy_action != "continue_current_plan":
                raise ValueError(
                    "intent_type='continue' requires legacy_action='continue_current_plan'."
                )
            if self.mutation_scope != "none":
                raise ValueError(
                    "intent_type='continue' requires mutation_scope='none'."
                )
            if self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='continue' cannot require plan mutation."
                )
            if self.requires_manual_review or self.should_close_stage:
                raise ValueError(
                    "intent_type='continue' cannot require manual review or close stage."
                )
            if not self.can_continue_after_application:
                raise ValueError(
                    "intent_type='continue' must allow continuation."
                )

        if self.intent_type == "assign":
            if self.legacy_action != "continue_current_plan":
                raise ValueError(
                    "intent_type='assign' keeps legacy_action='continue_current_plan'."
                )
            if self.mutation_scope != "assignment":
                raise ValueError(
                    "intent_type='assign' requires mutation_scope='assignment'."
                )
            if not self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='assign' must require plan mutation."
                )
            if not self.requires_all_new_tasks_assigned:
                raise ValueError(
                    "intent_type='assign' must require all new tasks to be assigned."
                )
            if not self.has_new_recovery_tasks:
                raise ValueError(
                    "intent_type='assign' requires has_new_recovery_tasks=True."
                )
            if self.requires_manual_review or self.should_close_stage:
                raise ValueError(
                    "intent_type='assign' cannot require manual review or close stage."
                )
            if not self.can_continue_after_application:
                raise ValueError(
                    "intent_type='assign' must allow continuation after application."
                )

        if self.intent_type == "resequence":
            if self.legacy_action != "resequence_remaining_batches":
                raise ValueError(
                    "intent_type='resequence' requires legacy_action='resequence_remaining_batches'."
                )
            if self.mutation_scope != "resequence":
                raise ValueError(
                    "intent_type='resequence' requires mutation_scope='resequence'."
                )
            if not self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='resequence' must require plan mutation."
                )
            if self.requires_manual_review or self.should_close_stage:
                raise ValueError(
                    "intent_type='resequence' cannot require manual review or close stage."
                )
            if self.can_continue_after_application:
                raise ValueError(
                    "intent_type='resequence' should not advance until the resequenced plan is applied."
                )

        if self.intent_type == "replan":
            if self.legacy_action != "replan_remaining_work":
                raise ValueError(
                    "intent_type='replan' requires legacy_action='replan_remaining_work'."
                )
            if self.mutation_scope != "replan":
                raise ValueError(
                    "intent_type='replan' requires mutation_scope='replan'."
                )
            if not self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='replan' must require plan mutation."
                )
            if self.remaining_plan_still_valid:
                raise ValueError(
                    "intent_type='replan' requires remaining_plan_still_valid=False."
                )
            if self.requires_manual_review or self.should_close_stage:
                raise ValueError(
                    "intent_type='replan' cannot require manual review or close stage."
                )
            if self.can_continue_after_application:
                raise ValueError(
                    "intent_type='replan' should not advance until the new plan is generated."
                )

        if self.intent_type == "manual_review":
            if self.legacy_action != "manual_review":
                raise ValueError(
                    "intent_type='manual_review' requires legacy_action='manual_review'."
                )
            if self.mutation_scope != "none":
                raise ValueError(
                    "intent_type='manual_review' requires mutation_scope='none'."
                )
            if self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='manual_review' cannot require plan mutation."
                )
            if not self.requires_manual_review:
                raise ValueError(
                    "intent_type='manual_review' requires requires_manual_review=True."
                )
            if self.can_continue_after_application or self.should_close_stage:
                raise ValueError(
                    "intent_type='manual_review' cannot continue or close stage."
                )

        if self.intent_type == "close":
            if self.legacy_action != "close_stage":
                raise ValueError(
                    "intent_type='close' requires legacy_action='close_stage'."
                )
            if self.mutation_scope != "none":
                raise ValueError(
                    "intent_type='close' requires mutation_scope='none'."
                )
            if self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='close' cannot require plan mutation."
                )
            if not self.should_close_stage:
                raise ValueError(
                    "intent_type='close' requires should_close_stage=True."
                )
            if self.requires_manual_review or self.can_continue_after_application:
                raise ValueError(
                    "intent_type='close' cannot require manual review or continuation."
                )
            if self.has_new_recovery_tasks and self.requires_all_new_tasks_assigned:
                raise ValueError(
                    "intent_type='close' is invalid while new recovery tasks still require assignment."
                )