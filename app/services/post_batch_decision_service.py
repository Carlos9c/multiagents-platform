from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas.post_batch_intent import ResolvedPostBatchIntent


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

    key_risks: list[str] | None = None
    notes: list[str] | None = None
    decision_signals: list[str] | None = None

    def __post_init__(self) -> None:
        self.key_risks = _normalize_string_list(self.key_risks)
        self.notes = _normalize_string_list(self.notes)
        self.decision_signals = _normalize_string_list(self.decision_signals)


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
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


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

    parts.extend(note for note in signals.notes or [] if note)
    parts.extend(item for item in extra if item)

    return " ".join(part.strip() for part in parts if part and part.strip()) or (
        "Checkpoint evaluation completed."
    )


def _append_signal(signals: PostBatchDecisionSignals, *extra_signals: str) -> list[str]:
    values = list(signals.decision_signals or [])
    for item in extra_signals:
        item = item.strip()
        if item and item not in values:
            values.append(item)
    return values


def _should_close_stage(signals: PostBatchDecisionSignals) -> bool:
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
    if not signals.remaining_plan_still_valid:
        return False

    if not (signals.new_recovery_tasks_created or signals.has_new_recovery_pending_tasks):
        return False

    if signals.new_recovery_tasks_blocking is True:
        return False

    if signals.has_preexisting_pending_valid_tasks:
        return True

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

    if signals.recommended_next_action != "continue_current_plan":
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
    if signals.manual_review_required or signals.decision == "manual_review_required":
        return ResolvedPostBatchIntent(
            intent_type="manual_review",
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
            decision_signals=list(signals.decision_signals or []),
        )

    if _is_structural_replan(signals):
        return ResolvedPostBatchIntent(
            intent_type="replan",
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
            decision_signals=list(signals.decision_signals or []),
        )

    if _is_local_resequence(signals):
        return ResolvedPostBatchIntent(
            intent_type="resequence",
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
            decision_signals=list(signals.decision_signals or []),
        )

    if _should_assign_new_work(signals):
        return ResolvedPostBatchIntent(
            intent_type="assign",
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
            decision_signals=list(signals.decision_signals or []),
        )

    if _should_close_stage(signals):
        return ResolvedPostBatchIntent(
            intent_type="close",
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
            decision_signals=list(signals.decision_signals or []),
        )

    if _should_continue_current_plan(signals):
        return ResolvedPostBatchIntent(
            intent_type="continue",
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
            decision_signals=list(signals.decision_signals or []),
        )

    return ResolvedPostBatchIntent(
        intent_type="manual_review",
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