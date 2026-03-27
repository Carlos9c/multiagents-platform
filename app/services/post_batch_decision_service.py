from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.schemas.post_batch_intent import (
    LegacyResolvedPostBatchAction,
    ResolvedPostBatchIntent,
)


ResolvedPostBatchAction = LegacyResolvedPostBatchAction


@dataclass(kw_only=True)
class PostBatchDecisionSignals:
    decision: str
    decision_summary: str

    project_stage_closed: bool
    manual_review_required: bool

    remaining_plan_still_valid: bool
    plan_change_scope: str
    recommended_next_action: str
    recommended_next_action_reason: str

    replan_required: bool
    replan_level: str

    followup_atomic_tasks_required: bool
    recovery_strategy: str

    new_recovery_tasks_created: bool
    new_recovery_tasks_blocking: bool | None
    single_task_tail_risk: bool

    has_pending_valid_tasks: bool
    remaining_batch_count: int
    is_final_batch: bool

    has_preexisting_pending_valid_tasks: bool = False
    preexisting_pending_valid_task_count: int = 0
    has_new_recovery_pending_tasks: bool = False
    new_recovery_pending_task_count: int = 0

    key_risks: list[str]
    notes: list[str]
    decision_signals: list[str]


@dataclass(kw_only=True)
class ResolvedPostBatchDecision:
    """
    Transitional legacy adapter.

    Keep this while post_batch_service and workflow code still consume the older
    booleans. The canonical resolution is ResolvedPostBatchIntent.
    """

    action: ResolvedPostBatchAction
    continue_execution: bool
    requires_replanning: bool
    requires_resequencing: bool
    requires_manual_review: bool
    is_stage_closed: bool
    reopened_finalization: bool
    notes: str


def _read_attr(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return bool(value)


def _normalize_string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    value = str(value).strip()
    return value or default


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def build_post_batch_decision_signals(
    *,
    evaluation_decision: Any,
    recovery_context: Any,
    has_pending_valid_tasks: bool,
    remaining_batch_count: int,
    is_final_batch: bool,
) -> PostBatchDecisionSignals:
    replan = _read_attr(evaluation_decision, "replan", None)

    recovery_created_tasks = _read_attr(recovery_context, "recovery_created_tasks", []) or []
    new_recovery_tasks_created = len(recovery_created_tasks) > 0

    return PostBatchDecisionSignals(
        decision=_normalize_string(_read_attr(evaluation_decision, "decision"), ""),
        decision_summary=_normalize_string(
            _read_attr(evaluation_decision, "decision_summary"),
            "Checkpoint evaluation completed.",
        ),
        project_stage_closed=_normalize_bool(
            _read_attr(evaluation_decision, "project_stage_closed"),
            False,
        ),
        manual_review_required=_normalize_bool(
            _read_attr(evaluation_decision, "manual_review_required"),
            False,
        ),
        remaining_plan_still_valid=_normalize_bool(
            _read_attr(evaluation_decision, "remaining_plan_still_valid"),
            True,
        ),
        plan_change_scope=_normalize_string(
            _read_attr(evaluation_decision, "plan_change_scope"),
            "none",
        ),
        recommended_next_action=_normalize_string(
            _read_attr(evaluation_decision, "recommended_next_action"),
            "",
        ),
        recommended_next_action_reason=_normalize_string(
            _read_attr(evaluation_decision, "recommended_next_action_reason"),
            "",
        ),
        replan_required=_normalize_bool(_read_attr(replan, "required"), False),
        replan_level=_normalize_string(_read_attr(replan, "level"), ""),
        followup_atomic_tasks_required=_normalize_bool(
            _read_attr(evaluation_decision, "followup_atomic_tasks_required"),
            False,
        ),
        recovery_strategy=_normalize_string(
            _read_attr(evaluation_decision, "recovery_strategy"),
            "none",
        ),
        new_recovery_tasks_created=new_recovery_tasks_created,
        new_recovery_tasks_blocking=_read_attr(
            evaluation_decision,
            "new_recovery_tasks_blocking",
            None,
        ),
        single_task_tail_risk=_normalize_bool(
            _read_attr(evaluation_decision, "single_task_tail_risk"),
            False,
        ),
        has_pending_valid_tasks=has_pending_valid_tasks,
        remaining_batch_count=remaining_batch_count,
        is_final_batch=is_final_batch,
        key_risks=_normalize_string_list(_read_attr(evaluation_decision, "key_risks", [])),
        notes=_normalize_string_list(_read_attr(evaluation_decision, "notes", [])),
        decision_signals=_normalize_string_list(
            _read_attr(evaluation_decision, "decision_signals", [])
        ),
    )


def _join_notes(signals: PostBatchDecisionSignals, *extra: str) -> str:
    parts: list[str] = []

    if signals.decision_summary:
        parts.append(signals.decision_summary)

    if signals.recommended_next_action_reason:
        parts.append(signals.recommended_next_action_reason)

    parts.extend(note for note in signals.notes if note)
    parts.extend(item for item in extra if item)

    return " ".join(part.strip() for part in parts if part and part.strip()) or (
        "Checkpoint evaluation completed."
    )


def _append_signal(signals: PostBatchDecisionSignals, *extra_signals: str) -> list[str]:
    values = list(signals.decision_signals)
    for item in extra_signals:
        item = item.strip()
        if item and item not in values:
            values.append(item)
    return values


def _should_close_stage(signals: PostBatchDecisionSignals) -> bool:
    """
    Close only when closure is actually legal.

    A stage may close only if:
    - evaluator requested closure,
    - this is the final batch of the current live plan,
    - there are no remaining batches,
    - and there is no newly created recovery work that still needs assignment.
    """
    evaluator_requests_close = (
        signals.project_stage_closed
        or signals.decision == "stage_completed"
        or signals.recommended_next_action == "close_stage"
    )

    if not evaluator_requests_close:
        return False

    if not signals.is_final_batch:
        return False

    if signals.remaining_batch_count > 0:
        return False

    if signals.has_new_recovery_pending_tasks or signals.new_recovery_tasks_created:
        return False

    return True


def _is_structural_replan(signals: PostBatchDecisionSignals) -> bool:
    return (
        not signals.remaining_plan_still_valid
        or signals.plan_change_scope == "high_level_replan"
        or (signals.replan_required and signals.replan_level == "high_level")
        or signals.recommended_next_action == "replan_remaining_work"
        or signals.recovery_strategy == "replan_from_high_level"
    )


def _is_local_resequence(signals: PostBatchDecisionSignals) -> bool:
    explicit_local_resequence = (
        signals.plan_change_scope in {"local_resequencing", "remaining_plan_rebuild"}
        or signals.recommended_next_action == "resequence_remaining_batches"
        or signals.new_recovery_tasks_blocking is True
        or (
            signals.has_new_recovery_pending_tasks
            and signals.followup_atomic_tasks_required
            and not signals.has_preexisting_pending_valid_tasks
        )
    )

    weak_tail_risk_only = (
        signals.has_new_recovery_pending_tasks
        and signals.single_task_tail_risk
        and not signals.has_preexisting_pending_valid_tasks
    )

    return explicit_local_resequence or weak_tail_risk_only


def _should_assign_new_work(signals: PostBatchDecisionSignals) -> bool:
    """
    Assignment is the canonical continuation path when:
    - the remaining plan is still valid,
    - there is newly created recovery work,
    - that work does not block immediate progress strongly enough to require resequence,
    - and we must not leave any new task unassigned before the next batch.
    """
    if not signals.remaining_plan_still_valid:
        return False

    if not (signals.new_recovery_tasks_created or signals.has_new_recovery_pending_tasks):
        return False

    if signals.new_recovery_tasks_blocking is True:
        return False

    if signals.recommended_next_action in {
        "manual_review",
        "resequence_remaining_batches",
        "replan_remaining_work",
        "close_stage",
    }:
        return False

    if signals.plan_change_scope in {"local_resequencing", "remaining_plan_rebuild", "high_level_replan"}:
        return False

    return True


def _should_continue_current_plan(signals: PostBatchDecisionSignals) -> bool:
    if not signals.remaining_plan_still_valid:
        return False

    if signals.new_recovery_tasks_created or signals.has_new_recovery_pending_tasks:
        return False

    if signals.new_recovery_tasks_blocking is True:
        return False

    if signals.recommended_next_action in {
        "manual_review",
        "resequence_remaining_batches",
        "replan_remaining_work",
        "close_stage",
    }:
        return False

    if signals.plan_change_scope in {
        "local_resequencing",
        "remaining_plan_rebuild",
        "high_level_replan",
    }:
        return False

    return True


def resolve_post_batch_intent(
    signals: PostBatchDecisionSignals,
) -> ResolvedPostBatchIntent:
    # 1. Manual review wins immediately.
    if signals.manual_review_required or signals.decision == "manual_review_required":
        return ResolvedPostBatchIntent(
            intent_type="manual_review",
            legacy_action="manual_review",
            mutation_scope="none",
            remaining_plan_still_valid=signals.remaining_plan_still_valid,
            has_new_recovery_tasks=(
                signals.new_recovery_tasks_created or signals.has_new_recovery_pending_tasks
            ),
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=False,
            should_close_stage=False,
            requires_manual_review=True,
            reopened_finalization=False,
            notes=_join_notes(
                signals,
                "Manual review is required before the workflow can continue.",
            ),
            decision_signals=list(signals.decision_signals),
        )

    # 2. Structural replan.
    if _is_structural_replan(signals):
        return ResolvedPostBatchIntent(
            intent_type="replan",
            legacy_action="replan_remaining_work",
            mutation_scope="replan",
            remaining_plan_still_valid=False,
            has_new_recovery_tasks=(
                signals.new_recovery_tasks_created or signals.has_new_recovery_pending_tasks
            ),
            requires_plan_mutation=True,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=False,
            should_close_stage=False,
            requires_manual_review=False,
            reopened_finalization=True,
            notes=_join_notes(
                signals,
                "The remaining plan is no longer structurally valid and must be rebuilt.",
            ),
            decision_signals=list(signals.decision_signals),
        )

    # 3. Local resequence / immediate blocking patch semantics.
    if _is_local_resequence(signals):
        return ResolvedPostBatchIntent(
            intent_type="resequence",
            legacy_action="resequence_remaining_batches",
            mutation_scope="resequence",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=(
                signals.new_recovery_tasks_created or signals.has_new_recovery_pending_tasks
            ),
            requires_plan_mutation=True,
            requires_all_new_tasks_assigned=(
                signals.new_recovery_tasks_created or signals.has_new_recovery_pending_tasks
            ),
            can_continue_after_application=False,
            should_close_stage=False,
            requires_manual_review=False,
            reopened_finalization=True,
            notes=_join_notes(
                signals,
                "The remaining plan is still valid, but the pending execution order must be adjusted locally.",
            ),
            decision_signals=list(signals.decision_signals),
        )

    # 4. Controlled assignment of newly created work into the live plan.
    if _should_assign_new_work(signals):
        return ResolvedPostBatchIntent(
            intent_type="assign",
            legacy_action="continue_current_plan",
            mutation_scope="assignment",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=True,
            requires_plan_mutation=True,
            requires_all_new_tasks_assigned=True,
            can_continue_after_application=True,
            should_close_stage=False,
            requires_manual_review=False,
            reopened_finalization=signals.is_final_batch,
            notes=_join_notes(
                signals,
                "New recovery work must be assigned into the active plan before the next batch starts.",
            ),
            decision_signals=list(signals.decision_signals),
        )

    # 5. Legal stage closure.
    if _should_close_stage(signals):
        return ResolvedPostBatchIntent(
            intent_type="close",
            legacy_action="close_stage",
            mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=False,
            should_close_stage=True,
            requires_manual_review=False,
            reopened_finalization=False,
            notes=_join_notes(
                signals,
                "The current live plan is exhausted and the stage can be closed.",
            ),
            decision_signals=list(signals.decision_signals),
        )

    # 6. Plain continuation with no new recovery work to place.
    if _should_continue_current_plan(signals):
        return ResolvedPostBatchIntent(
            intent_type="continue",
            legacy_action="continue_current_plan",
            mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=True,
            should_close_stage=False,
            requires_manual_review=False,
            reopened_finalization=False,
            notes=_join_notes(
                signals,
                "The current remaining plan is still valid and can continue without mutation.",
            ),
            decision_signals=list(signals.decision_signals),
        )

    # 7. Conservative fallback.
    return ResolvedPostBatchIntent(
        intent_type="manual_review",
        legacy_action="manual_review",
        mutation_scope="none",
        remaining_plan_still_valid=signals.remaining_plan_still_valid,
        has_new_recovery_tasks=(
            signals.new_recovery_tasks_created or signals.has_new_recovery_pending_tasks
        ),
        requires_plan_mutation=False,
        requires_all_new_tasks_assigned=False,
        can_continue_after_application=False,
        should_close_stage=False,
        requires_manual_review=True,
        reopened_finalization=False,
        notes=_join_notes(
            signals,
            "The checkpoint signals were not strong enough to continue automatically.",
        ),
        decision_signals=_append_signal(signals, "manual_review_fallback"),
    )


def resolve_post_batch_decision(
    signals: PostBatchDecisionSignals,
) -> ResolvedPostBatchDecision:
    """
    Legacy adapter.

    Keep this function during the migration so existing callers still work while
    the rest of the workflow moves to ResolvedPostBatchIntent.
    """
    intent = resolve_post_batch_intent(signals)

    return ResolvedPostBatchDecision(
        action=intent.legacy_action,
        continue_execution=(
            intent.intent_type in {"continue", "assign"}
            and intent.can_continue_after_application
        ),
        requires_replanning=intent.intent_type == "replan",
        requires_resequencing=intent.intent_type == "resequence",
        requires_manual_review=intent.intent_type == "manual_review",
        is_stage_closed=intent.intent_type == "close",
        reopened_finalization=intent.reopened_finalization,
        notes=intent.notes,
    )