from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ResolvedPostBatchAction = Literal[
    "continue_current_plan",
    "resequence_remaining_batches",
    "replan_remaining_work",
    "manual_review",
    "close_stage",
]


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

    return " ".join(part.strip() for part in parts if part and part.strip()) or "Checkpoint evaluation completed."


def resolve_post_batch_decision(
    signals: PostBatchDecisionSignals,
) -> ResolvedPostBatchDecision:
    # 1. Cierre de stage
    if signals.project_stage_closed or signals.decision == "stage_completed":
        return ResolvedPostBatchDecision(
            action="close_stage",
            continue_execution=False,
            requires_replanning=False,
            requires_resequencing=False,
            requires_manual_review=False,
            is_stage_closed=True,
            reopened_finalization=False,
            notes=_join_notes(signals),
        )

    # 2. Revisión manual
    if signals.manual_review_required or signals.decision == "manual_review_required":
        return ResolvedPostBatchDecision(
            action="manual_review",
            continue_execution=False,
            requires_replanning=False,
            requires_resequencing=False,
            requires_manual_review=True,
            is_stage_closed=False,
            reopened_finalization=False,
            notes=_join_notes(signals),
        )

    # 3. Replan estructural
    structural_replan = (
        not signals.remaining_plan_still_valid
        or signals.plan_change_scope == "high_level_replan"
        or (signals.replan_required and signals.replan_level == "high_level")
        or signals.recommended_next_action == "replan_remaining_work"
        or signals.recovery_strategy == "replan_from_high_level"
    )

    if structural_replan:
        return ResolvedPostBatchDecision(
            action="replan_remaining_work",
            continue_execution=False,
            requires_replanning=True,
            requires_resequencing=False,
            requires_manual_review=False,
            is_stage_closed=False,
            reopened_finalization=True,
            notes=_join_notes(
                signals,
                "The remaining plan is no longer structurally valid and must be rebuilt.",
            ),
        )

    # 4. Resequencing local / inserción operativa de trabajo nuevo
    local_resequence = (
        signals.plan_change_scope in {"local_resequencing", "remaining_plan_rebuild"}
        or signals.recommended_next_action == "resequence_remaining_batches"
        or signals.new_recovery_tasks_blocking is True
        or (
            signals.has_new_recovery_pending_tasks
            and signals.followup_atomic_tasks_required
            and not signals.has_preexisting_pending_valid_tasks
        )
        or (
            signals.has_new_recovery_pending_tasks
            and signals.single_task_tail_risk
        )
    )

    if local_resequence:
        return ResolvedPostBatchDecision(
            action="resequence_remaining_batches",
            continue_execution=False,
            requires_replanning=False,
            requires_resequencing=True,
            requires_manual_review=False,
            is_stage_closed=False,
            reopened_finalization=True,
            notes=_join_notes(
                signals,
                "The remaining plan is still valid, but the pending execution order must be adjusted.",
            ),
        )

    # 5. Continuidad por defecto cuando el plan sigue siendo válido
    if (
        signals.remaining_plan_still_valid
        and signals.new_recovery_tasks_blocking is not True
        and signals.recommended_next_action not in {
            "manual_review",
            "resequence_remaining_batches",
            "replan_remaining_work",
        }
    ):
        return ResolvedPostBatchDecision(
            action="continue_current_plan",
            continue_execution=True,
            requires_replanning=False,
            requires_resequencing=False,
            requires_manual_review=False,
            is_stage_closed=False,
            reopened_finalization=False,
            notes=_join_notes(
                signals,
                "The current remaining plan is still valid and execution can continue without structural changes.",
            ),
        )
    
    # 6. Fallback conservador
    return ResolvedPostBatchDecision(
        action="manual_review",
        continue_execution=False,
        requires_replanning=False,
        requires_resequencing=False,
        requires_manual_review=True,
        is_stage_closed=False,
        reopened_finalization=False,
        notes=_join_notes(
            signals,
            "The checkpoint signals were not strong enough to continue automatically.",
        ),
    )