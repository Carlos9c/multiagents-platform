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


@dataclass(frozen=True, kw_only=True)
class ResolvedPostBatchIntent:
    """
    Canonical post-batch intent.

    This object is the single resolved interpretation of:
    - evaluator signals
    - recovery side-effects
    - pending work state
    - end-of-plan semantics

    It is the only contract that post-batch orchestration should consume in
    order to decide what happens next.
    """

    intent_type: ResolvedPostBatchIntentType
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
        normalized_notes = self.notes.strip()
        if not normalized_notes:
            raise ValueError("ResolvedPostBatchIntent.notes cannot be empty.")
        object.__setattr__(self, "notes", normalized_notes)

        if self.decision_signals is None:
            raise ValueError(
                "ResolvedPostBatchIntent.decision_signals cannot be None."
            )

        normalized_signals: list[str] = []
        seen: set[str] = set()
        for signal in self.decision_signals:
            normalized = signal.strip()
            if not normalized or normalized in seen:
                continue
            normalized_signals.append(normalized)
            seen.add(normalized)
        object.__setattr__(self, "decision_signals", normalized_signals)

        if self.mutation_scope == "none" and self.requires_plan_mutation:
            raise ValueError(
                "mutation_scope='none' is incompatible with requires_plan_mutation=True."
            )

        if self.mutation_scope != "none" and not self.requires_plan_mutation:
            raise ValueError(
                "mutation_scope!='none' requires requires_plan_mutation=True."
            )

        if self.requires_all_new_tasks_assigned and not self.has_new_recovery_tasks:
            raise ValueError(
                "requires_all_new_tasks_assigned=True requires has_new_recovery_tasks=True."
            )

        if self.should_close_stage and self.intent_type != "close":
            raise ValueError(
                "should_close_stage=True requires intent_type='close'."
            )

        if self.requires_manual_review and self.intent_type != "manual_review":
            raise ValueError(
                "requires_manual_review=True requires intent_type='manual_review'."
            )

        if self.reopened_finalization and self.intent_type in {
            "continue",
            "manual_review",
            "close",
        }:
            raise ValueError(
                "reopened_finalization=True is incompatible with intent_type "
                "in {'continue', 'manual_review', 'close'}."
            )

        if self.intent_type == "continue":
            if self.mutation_scope != "none":
                raise ValueError(
                    "intent_type='continue' requires mutation_scope='none'."
                )
            if self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='continue' cannot require plan mutation."
                )
            if self.has_new_recovery_tasks:
                raise ValueError(
                    "intent_type='continue' cannot carry new recovery tasks."
                )
            if self.requires_all_new_tasks_assigned:
                raise ValueError(
                    "intent_type='continue' cannot require new task assignment."
                )
            if self.requires_manual_review:
                raise ValueError(
                    "intent_type='continue' cannot require manual review."
                )
            if self.should_close_stage:
                raise ValueError(
                    "intent_type='continue' cannot close the stage."
                )
            if self.reopened_finalization:
                raise ValueError(
                    "intent_type='continue' cannot reopen finalization."
                )
            if not self.can_continue_after_application:
                raise ValueError(
                    "intent_type='continue' must allow continuation."
                )

        elif self.intent_type == "assign":
            if self.mutation_scope != "assignment":
                raise ValueError(
                    "intent_type='assign' requires mutation_scope='assignment'."
                )
            if not self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='assign' must require plan mutation."
                )
            if not self.has_new_recovery_tasks:
                raise ValueError(
                    "intent_type='assign' requires has_new_recovery_tasks=True."
                )
            if not self.requires_all_new_tasks_assigned:
                raise ValueError(
                    "intent_type='assign' must require all new tasks to be assigned."
                )
            if self.requires_manual_review:
                raise ValueError(
                    "intent_type='assign' cannot require manual review."
                )
            if self.should_close_stage:
                raise ValueError(
                    "intent_type='assign' cannot close the stage."
                )
            if not self.can_continue_after_application:
                raise ValueError(
                    "intent_type='assign' must allow continuation after application."
                )

        elif self.intent_type == "resequence":
            if self.mutation_scope != "resequence":
                raise ValueError(
                    "intent_type='resequence' requires mutation_scope='resequence'."
                )
            if not self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='resequence' must require plan mutation."
                )
            if self.requires_manual_review:
                raise ValueError(
                    "intent_type='resequence' cannot require manual review."
                )
            if self.should_close_stage:
                raise ValueError(
                    "intent_type='resequence' cannot close the stage."
                )
            if self.can_continue_after_application:
                raise ValueError(
                    "intent_type='resequence' must not continue before applying the resequenced plan."
                )

        elif self.intent_type == "replan":
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
            if self.requires_manual_review:
                raise ValueError(
                    "intent_type='replan' cannot require manual review."
                )
            if self.should_close_stage:
                raise ValueError(
                    "intent_type='replan' cannot close the stage."
                )
            if self.can_continue_after_application:
                raise ValueError(
                    "intent_type='replan' must not continue before generating the new plan."
                )

        elif self.intent_type == "manual_review":
            if self.mutation_scope != "none":
                raise ValueError(
                    "intent_type='manual_review' requires mutation_scope='none'."
                )
            if self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='manual_review' cannot require plan mutation."
                )
            if self.requires_all_new_tasks_assigned:
                raise ValueError(
                    "intent_type='manual_review' cannot require assignment as an automatic action."
                )
            if not self.requires_manual_review:
                raise ValueError(
                    "intent_type='manual_review' requires requires_manual_review=True."
                )
            if self.can_continue_after_application:
                raise ValueError(
                    "intent_type='manual_review' cannot continue automatically."
                )
            if self.should_close_stage:
                raise ValueError(
                    "intent_type='manual_review' cannot close the stage."
                )
            if self.reopened_finalization:
                raise ValueError(
                    "intent_type='manual_review' cannot reopen finalization by itself."
                )

        elif self.intent_type == "close":
            if self.mutation_scope != "none":
                raise ValueError(
                    "intent_type='close' requires mutation_scope='none'."
                )
            if self.requires_plan_mutation:
                raise ValueError(
                    "intent_type='close' cannot require plan mutation."
                )
            if self.requires_all_new_tasks_assigned:
                raise ValueError(
                    "intent_type='close' cannot require assignment of new work."
                )
            if self.has_new_recovery_tasks:
                raise ValueError(
                    "intent_type='close' is invalid while new recovery tasks still exist."
                )
            if not self.should_close_stage:
                raise ValueError(
                    "intent_type='close' requires should_close_stage=True."
                )
            if self.requires_manual_review:
                raise ValueError(
                    "intent_type='close' cannot require manual review."
                )
            if self.can_continue_after_application:
                raise ValueError(
                    "intent_type='close' cannot continue execution."
                )
            if self.reopened_finalization:
                raise ValueError(
                    "intent_type='close' cannot reopen finalization."
                )

        else:
            raise ValueError(f"Unsupported intent_type: {self.intent_type}")