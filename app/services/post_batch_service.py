import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import (
    EXECUTION_RUN_STATUS_FAILED,
    EXECUTION_RUN_STATUS_PARTIAL,
    EXECUTION_RUN_STATUS_PENDING,
    EXECUTION_RUN_STATUS_REJECTED,
    EXECUTION_RUN_STATUS_RUNNING,
    EXECUTION_RUN_STATUS_SUCCEEDED,
    ExecutionRun,
)
from app.models.project import Project
from app.models.task import (
    TASK_STATUS_AWAITING_VALIDATION,
    TASK_STATUS_PENDING,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.schemas.workflow_iteration_trace import WorkflowIterationTrace
from app.schemas.execution_plan import ExecutionBatch, ExecutionPlan
from app.schemas.post_batch import PostBatchResult, PostBatchTaskRunSummary
from app.schemas.recovery import RecoveryContext
from app.services.artifacts import create_artifact
from app.services.evaluation_service import evaluate_checkpoint, persist_evaluation_decision
from app.services.recovery_service import (
    build_recovery_context_entry,
    generate_recovery_decision,
    materialize_recovery_decision,
    merge_recovery_contexts,
    persist_recovery_decision,
)
from app.services.task_hierarchy_reconciliation_service import (
    TaskHierarchyReconciliationServiceError,
    reconcile_task_hierarchy_after_changes,
)
from app.services.post_batch_decision_service import (
    build_post_batch_decision_signals,
    resolve_post_batch_decision,
)
from app.services.execution_plan_patch_service import (
    insert_patch_batch_after_batch,
    persist_patched_execution_plan,
)

TERMINAL_RUN_STATUSES = {
    EXECUTION_RUN_STATUS_SUCCEEDED,
    EXECUTION_RUN_STATUS_PARTIAL,
    EXECUTION_RUN_STATUS_FAILED,
    EXECUTION_RUN_STATUS_REJECTED,
}

NON_TERMINAL_RUN_STATUSES = {
    EXECUTION_RUN_STATUS_PENDING,
    EXECUTION_RUN_STATUS_RUNNING,
}

CODE_VALIDATION_RESULT_ARTIFACT_TYPE = "code_validation_result"


class PostBatchServiceError(Exception):
    """Base exception for post-batch orchestration errors."""


@dataclass
class NormalizedEvaluationOutcome:
    continue_execution: bool
    requires_replanning: bool
    requires_resequencing: bool
    requires_manual_review: bool
    is_stage_closed: bool
    reopened_finalization: bool
    notes: str

def _serialize_workflow_iteration_trace(trace: WorkflowIterationTrace) -> str:
    return json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _build_workflow_iteration_trace(
    *,
    project_id: int,
    batch: ExecutionBatch,
    checkpoint_id: str,
    created_recovery_task_ids: list[int],
    result: PostBatchResult,
) -> WorkflowIterationTrace:
    return WorkflowIterationTrace(
        project_id=project_id,
        plan_version=result.plan_version,
        batch_internal_id=batch.batch_internal_id,
        batch_id=batch.batch_id,
        batch_index=batch.batch_index,
        checkpoint_id=checkpoint_id,
        executed_task_ids=list(result.executed_task_ids),
        successful_task_ids=list(result.successful_task_ids),
        problematic_run_ids=list(result.problematic_run_ids),
        created_recovery_task_ids=list(created_recovery_task_ids),
        resolved_action=result.resolved_action,
        decision_signals_used=list(result.decision_signals_used),
        continue_execution=result.continue_execution,
        requires_resequencing=result.requires_resequencing,
        requires_replanning=result.requires_replanning,
        requires_manual_review=result.requires_manual_review,
        is_final_batch=result.is_final_batch,
        finalization_iteration_count=result.finalization_iteration_count,
        max_finalization_iterations=result.max_finalization_iterations,
        finalization_guard_triggered=result.finalization_guard_triggered,
        notes=result.notes,
    )


def _persist_workflow_iteration_trace(
    db: Session,
    *,
    project_id: int,
    trace: WorkflowIterationTrace,
    created_by: str = "post_batch_processor",
):
    return create_artifact(
        db=db,
        project_id=project_id,
        task_id=None,
        artifact_type="workflow_iteration_trace",
        content=_serialize_workflow_iteration_trace(trace),
        created_by=created_by,
    )


def _serialize_post_batch_result(result: PostBatchResult) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _get_batch(plan: ExecutionPlan, batch_id: str) -> ExecutionBatch:
    batch = next((batch for batch in plan.execution_batches if batch.batch_id == batch_id), None)
    if not batch:
        raise PostBatchServiceError(
            f"Batch '{batch_id}' not found in execution plan version {plan.plan_version}"
        )
    return batch


def _get_checkpoint_for_batch(plan: ExecutionPlan, batch_id: str):
    checkpoint = next((cp for cp in plan.checkpoints if cp.after_batch_id == batch_id), None)
    if checkpoint is None:
        raise PostBatchServiceError(
            f"Batch '{batch_id}' has no checkpoint associated in execution plan version "
            f"{plan.plan_version}. Every batch processed by post-batch must close with an explicit checkpoint."
        )
    return checkpoint


def _get_latest_run_for_task(db: Session, task_id: int) -> ExecutionRun | None:
    return (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )


def _get_latest_validation_artifact_for_task(
    db: Session,
    task_id: int,
) -> Artifact | None:
    return (
        db.query(Artifact)
        .filter(
            Artifact.task_id == task_id,
            Artifact.artifact_type == CODE_VALIDATION_RESULT_ARTIFACT_TYPE,
        )
        .order_by(Artifact.id.desc())
        .first()
    )


def _require_task_is_ready_for_post_batch(
    *,
    task: Task,
    batch_id: str,
    plan_version: int,
) -> None:
    if task.status == TASK_STATUS_AWAITING_VALIDATION:
        raise PostBatchServiceError(
            f"Batch '{batch_id}' in plan version {plan_version} cannot be processed because "
            f"task {task.id} is still awaiting validation."
        )

    if task.status not in TERMINAL_TASK_STATUSES:
        raise PostBatchServiceError(
            f"Batch '{batch_id}' in plan version {plan_version} cannot be processed because "
            f"task {task.id} is in non-terminal status '{task.status}'."
        )


def _require_terminal_run_for_task(
    db: Session,
    *,
    task: Task,
    batch_id: str,
    plan_version: int,
) -> ExecutionRun:
    latest_run = _get_latest_run_for_task(db, task.id)
    if latest_run is None:
        raise PostBatchServiceError(
            f"Batch '{batch_id}' in plan version {plan_version} cannot be processed because "
            f"task {task.id} has no execution run."
        )

    if latest_run.status in NON_TERMINAL_RUN_STATUSES:
        raise PostBatchServiceError(
            f"Batch '{batch_id}' in plan version {plan_version} cannot be processed because "
            f"task {task.id} latest run {latest_run.id} is still '{latest_run.status}'."
        )

    if latest_run.status not in TERMINAL_RUN_STATUSES:
        raise PostBatchServiceError(
            f"Batch '{batch_id}' in plan version {plan_version} cannot be processed because "
            f"task {task.id} latest run {latest_run.id} has unsupported status '{latest_run.status}'."
        )

    return latest_run


def _require_validation_artifact_for_problematic_task(
    db: Session,
    *,
    task: Task,
    batch_id: str,
    plan_version: int,
) -> Artifact:
    validation_artifact = _get_latest_validation_artifact_for_task(db, task.id)
    if validation_artifact is None:
        raise PostBatchServiceError(
            f"Batch '{batch_id}' in plan version {plan_version} cannot be processed because "
            f"task {task.id} is '{task.status}' but has no '{CODE_VALIDATION_RESULT_ARTIFACT_TYPE}' artifact."
        )
    return validation_artifact


def _require_recovery_source_task_remains_terminal(
    db: Session,
    *,
    source_task_id: int,
    batch_id: str,
    plan_version: int,
) -> Task:
    refreshed_task = db.get(Task, source_task_id)
    if refreshed_task is None:
        raise PostBatchServiceError(
            f"Batch '{batch_id}' in plan version {plan_version} lost source task {source_task_id} after recovery materialization."
        )

    if refreshed_task.status not in {TASK_STATUS_FAILED, TASK_STATUS_PARTIAL}:
        raise PostBatchServiceError(
            f"Recovery integrity error in batch '{batch_id}' plan version {plan_version}: "
            f"source task {refreshed_task.id} ended with invalid status '{refreshed_task.status}' "
            "after recovery materialization. The original atomic task must remain terminal."
        )

    return refreshed_task


def _get_artifact_ids_for_tasks(
    db: Session,
    project_id: int,
    task_ids: list[int],
) -> list[int]:
    if not task_ids:
        return []

    artifacts = (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.task_id.in_(task_ids),
        )
        .order_by(Artifact.id.asc())
        .all()
    )
    return [artifact.id for artifact in artifacts]

def _get_artifact_ids_in_checkpoint_window(
    db: Session,
    *,
    project_id: int,
    start_exclusive: int,
) -> list[int]:
    artifacts = (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.id > start_exclusive,
        )
        .order_by(Artifact.id.asc())
        .all()
    )
    return [artifact.id for artifact in artifacts]


def _build_next_batch_summary(plan: ExecutionPlan, batch_id: str) -> str | None:
    batch_index = next(
        (index for index, batch in enumerate(plan.execution_batches) if batch.batch_id == batch_id),
        None,
    )
    if batch_index is None:
        return None

    if batch_index + 1 >= len(plan.execution_batches):
        return None

    next_batch = plan.execution_batches[batch_index + 1]
    return json.dumps(next_batch.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _build_remaining_plan_summary(plan: ExecutionPlan, batch_id: str) -> str | None:
    batch_index = next(
        (index for index, batch in enumerate(plan.execution_batches) if batch.batch_id == batch_id),
        None,
    )
    if batch_index is None:
        return None

    remaining_batches = plan.execution_batches[batch_index + 1 :]
    if not remaining_batches:
        return None

    payload = {
        "plan_version": plan.plan_version,
        "remaining_batches": [batch.model_dump(mode="json") for batch in remaining_batches],
        "blocked_task_ids": plan.blocked_task_ids,
        "sequencing_rationale": plan.sequencing_rationale,
        "uncertainties": plan.uncertainties,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_execution_context_summary(
    *,
    task: Task,
    latest_run: ExecutionRun,
) -> str:
    payload = {
        "task_id": task.id,
        "task_status": task.status,
        "latest_run": {
            "run_id": latest_run.id,
            "run_status": latest_run.status,
            "failure_type": latest_run.failure_type,
            "failure_code": latest_run.failure_code,
            "work_summary": latest_run.work_summary,
            "work_details": latest_run.work_details,
            "completed_scope": latest_run.completed_scope,
            "remaining_scope": latest_run.remaining_scope,
            "blockers_found": latest_run.blockers_found,
            "validation_notes": latest_run.validation_notes,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_validation_context_summary(
    *,
    task: Task,
    validation_artifact: Artifact,
) -> str:
    payload = {
        "task_id": task.id,
        "task_status": task.status,
        "validation_artifact": {
            "artifact_id": validation_artifact.id,
            "artifact_type": validation_artifact.artifact_type,
            "content": validation_artifact.content,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _persist_post_batch_result(
    db: Session,
    project_id: int,
    result: PostBatchResult,
    created_by: str = "post_batch_processor",
):
    return create_artifact(
        db=db,
        project_id=project_id,
        task_id=None,
        artifact_type="post_batch_result",
        content=_serialize_post_batch_result(result),
        created_by=created_by,
    )


def _count_valid_pending_tasks(
    db: Session,
    *,
    project_id: int,
    exclude_task_ids: list[int] | None = None,
) -> int:
    query = db.query(Task).filter(
        Task.project_id == project_id,
        Task.status == TASK_STATUS_PENDING,
        Task.is_blocked.is_(False),
    )

    if exclude_task_ids:
        query = query.filter(~Task.id.in_(exclude_task_ids))

    return query.count()


def _count_valid_pending_tasks_for_ids(
    db: Session,
    *,
    project_id: int,
    task_ids: list[int],
) -> int:
    if not task_ids:
        return 0

    return (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.id.in_(task_ids),
            Task.status == TASK_STATUS_PENDING,
            Task.is_blocked.is_(False),
        )
        .count()
    )


def _count_preexisting_valid_pending_tasks(
    db: Session,
    *,
    project_id: int,
    executed_task_ids: list[int],
    created_recovery_task_ids: list[int],
) -> int:
    exclude_ids = list(dict.fromkeys(executed_task_ids + created_recovery_task_ids))
    return _count_valid_pending_tasks(
        db=db,
        project_id=project_id,
        exclude_task_ids=exclude_ids,
    )

def _is_final_batch(plan: ExecutionPlan, batch_id: str) -> bool:
    if not plan.execution_batches:
        return False
    return plan.execution_batches[-1].batch_id == batch_id


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


def _is_new_stage_evaluation_output(evaluation_decision: Any) -> bool:
    return (
        hasattr(evaluation_decision, "decision")
        and hasattr(evaluation_decision, "project_stage_closed")
        and hasattr(evaluation_decision, "remaining_plan_still_valid")
        and hasattr(evaluation_decision, "recommended_next_action")
    )

def _build_notes(
    *,
    decision_summary: str,
    recommended_next_action_reason: str,
    notes_list: Any,
    fallback: str,
) -> str:
    parts: list[str] = []

    if decision_summary:
        parts.append(decision_summary)

    if recommended_next_action_reason:
        parts.append(recommended_next_action_reason)

    if isinstance(notes_list, list):
        parts.extend(str(item).strip() for item in notes_list if str(item).strip())

    notes = " ".join(parts).strip()
    return notes or fallback


def _normalize_legacy_evaluation_outcome(evaluation_decision: Any) -> NormalizedEvaluationOutcome:
    decision_type = _normalize_string(_read_attr(evaluation_decision, "decision_type"))
    continue_execution = _normalize_bool(
        _read_attr(evaluation_decision, "continue_execution"),
        default=False,
    )

    requires_replanning = decision_type == "replan_from_level"
    requires_resequencing = (
        decision_type in {"insert_new_tasks", "resequence_remaining_tasks", "replan_from_level"}
        or _normalize_bool(_read_attr(evaluation_decision, "resequence_remaining_tasks"), default=False)
    )
    requires_manual_review = decision_type == "manual_review"
    is_stage_closed = decision_type == "approve_continue" and not continue_execution

    if decision_type == "approve_continue":
        notes = "Legacy evaluator approved continuation or closure."
    elif decision_type == "request_corrections":
        notes = "Legacy evaluator requested corrections before continuing."
    elif decision_type == "insert_new_tasks":
        notes = "Legacy evaluator requested insertion of new tasks."
    elif decision_type == "resequence_remaining_tasks":
        notes = "Legacy evaluator requested resequencing of remaining tasks."
    elif decision_type == "replan_from_level":
        notes = "Legacy evaluator requested replanning from a prior level."
    elif decision_type == "manual_review":
        notes = "Legacy evaluator requested manual review."
    else:
        notes = "Legacy evaluator produced an unrecognized decision type."

    return NormalizedEvaluationOutcome(
        continue_execution=continue_execution,
        requires_replanning=requires_replanning,
        requires_resequencing=requires_resequencing,
        requires_manual_review=requires_manual_review,
        is_stage_closed=is_stage_closed,
        reopened_finalization=requires_resequencing or requires_replanning,
        notes=notes,
    )


def _normalize_new_evaluation_outcome(evaluation_decision: Any) -> NormalizedEvaluationOutcome:
    """
    Normalize the new stage evaluation output.

    Primary source of truth:
    - recommended_next_action

    Compatibility fallback:
    - if recommended_next_action is absent/empty, infer the operational action
      from decision/replan/followup/manual_review flags.
    """
    decision = _normalize_string(_read_attr(evaluation_decision, "decision"), "")
    decision_summary = _normalize_string(_read_attr(evaluation_decision, "decision_summary"), "")
    notes_list = _read_attr(evaluation_decision, "notes", None)

    project_stage_closed = _normalize_bool(
        _read_attr(evaluation_decision, "project_stage_closed"),
        False,
    )
    manual_review_required = _normalize_bool(
        _read_attr(evaluation_decision, "manual_review_required"),
        False,
    )
    followup_atomic_tasks_required = _normalize_bool(
        _read_attr(evaluation_decision, "followup_atomic_tasks_required"),
        False,
    )

    recovery_strategy = _normalize_string(
        _read_attr(evaluation_decision, "recovery_strategy"),
        "none",
    )

    replan = _read_attr(evaluation_decision, "replan", None)
    replan_required = False
    replan_level = ""
    if replan is not None:
        replan_required = _normalize_bool(_read_attr(replan, "required"), False)
        replan_level = _normalize_string(_read_attr(replan, "level"), "")

    recommended_next_action = _normalize_string(
        _read_attr(evaluation_decision, "recommended_next_action"),
        "",
    )
    recommended_next_action_reason = _normalize_string(
        _read_attr(evaluation_decision, "recommended_next_action_reason"),
        "",
    )

    is_stage_closed = project_stage_closed
    requires_manual_review = manual_review_required
    continue_execution = False
    requires_resequencing = False
    requires_replanning = False
    reopened_finalization = False

    if recommended_next_action == "close_stage":
        is_stage_closed = True
        continue_execution = False

    elif recommended_next_action == "manual_review":
        requires_manual_review = True
        continue_execution = False

    elif recommended_next_action == "continue_current_plan":
        continue_execution = True
        requires_replanning = False
        requires_resequencing = False

    elif recommended_next_action == "resequence_remaining_batches":
        continue_execution = False
        requires_resequencing = True
        requires_replanning = False
        reopened_finalization = True

    elif recommended_next_action == "replan_remaining_work":
        continue_execution = False
        requires_replanning = True
        requires_resequencing = False
        reopened_finalization = True

    else:
        # Defensive compatibility path for partially migrated outputs.
        if decision == "stage_completed":
            is_stage_closed = True
            continue_execution = False

        elif decision == "manual_review_required":
            requires_manual_review = True
            continue_execution = False

        elif decision == "stage_incomplete":
            if replan_required and replan_level == "high_level":
                requires_replanning = True
                continue_execution = False
                reopened_finalization = True
            elif (
                followup_atomic_tasks_required
                or recovery_strategy in {
                    "insert_followup_atomic_tasks",
                    "reatomize_failed_tasks",
                }
                or (replan_required and replan_level == "atomic")
            ):
                requires_resequencing = True
                continue_execution = False
                reopened_finalization = True
            elif requires_manual_review:
                continue_execution = False
            else:
                continue_execution = True

        else:
            raise PostBatchServiceError(
                "Checkpoint evaluation produced an unsupported StageEvaluationOutput decision "
                f"without a usable recommended_next_action. decision='{decision}'."
            )

    if requires_manual_review:
        continue_execution = False

    if is_stage_closed:
        continue_execution = False
        requires_replanning = False
        requires_resequencing = False
        reopened_finalization = False

    if requires_replanning and requires_resequencing:
        raise PostBatchServiceError(
            "Checkpoint evaluation normalization produced both replanning and resequencing. "
            "These actions must be mutually exclusive at post-batch orchestration time."
        )

    notes = _build_notes(
        decision_summary=decision_summary,
        recommended_next_action_reason=recommended_next_action_reason,
        notes_list=notes_list,
        fallback="Checkpoint evaluation completed.",
    )

    return NormalizedEvaluationOutcome(
        continue_execution=continue_execution,
        requires_replanning=requires_replanning,
        requires_resequencing=requires_resequencing,
        requires_manual_review=requires_manual_review,
        is_stage_closed=is_stage_closed,
        reopened_finalization=reopened_finalization,
        notes=notes,
    )


def _normalize_evaluation_outcome(evaluation_decision: Any) -> NormalizedEvaluationOutcome:
    if evaluation_decision is None:
        raise PostBatchServiceError("Checkpoint evaluation returned no decision object.")

    if _is_new_stage_evaluation_output(evaluation_decision):
        return _normalize_new_evaluation_outcome(evaluation_decision)

    return _normalize_legacy_evaluation_outcome(evaluation_decision)


def _should_materialize_patch_batch(
    *,
    normalized: NormalizedEvaluationOutcome,
    evaluation_decision: Any,
    created_recovery_task_ids: list[int],
) -> bool:
    if not created_recovery_task_ids:
        return False

    if not normalized.requires_resequencing or normalized.requires_replanning:
        return False

    if not _normalize_bool(_read_attr(evaluation_decision, "remaining_plan_still_valid"), True):
        return False

    if not _normalize_bool(_read_attr(evaluation_decision, "new_recovery_tasks_blocking"), False):
        return False

    return True


def _degrade_illegal_stage_closure_for_non_final_batch(
    *,
    normalized: NormalizedEvaluationOutcome,
    is_final_batch: bool,
) -> NormalizedEvaluationOutcome:
    if is_final_batch or not normalized.is_stage_closed:
        return normalized

    return NormalizedEvaluationOutcome(
        continue_execution=True,
        requires_replanning=False,
        requires_resequencing=False,
        requires_manual_review=False,
        is_stage_closed=False,
        reopened_finalization=False,
        notes=(
            "Evaluator proposed closing the stage at a non-final batch. "
            "Stage closure was deferred because only the final batch can close the stage in the current workflow."
        ),
    )


def _reconcile_hierarchy_after_batch_changes(
    db: Session,
    *,
    executed_task_ids: list[int],
    created_recovery_task_ids: list[int],
) -> None:
    affected_task_ids = list(dict.fromkeys(executed_task_ids + created_recovery_task_ids))

    try:
        reconcile_task_hierarchy_after_changes(
            db=db,
            affected_task_ids=affected_task_ids,
        )
    except TaskHierarchyReconciliationServiceError as exc:
        raise PostBatchServiceError(
            f"Post-batch hierarchy reconciliation failed: {str(exc)}"
        ) from exc


def process_batch_after_execution(
    db: Session,
    project_id: int,
    plan: ExecutionPlan,
    batch_id: str,
    persist_result: bool = True,
    finalization_iteration_count: int = 0,
    max_finalization_iterations: int = 2,
    checkpoint_artifact_window_start_exclusive: int | None = None,
) -> PostBatchResult:
    project = db.get(Project, project_id)
    if not project:
        raise PostBatchServiceError(f"Project {project_id} not found")

    batch = _get_batch(plan, batch_id)
    checkpoint = _get_checkpoint_for_batch(plan, batch_id)
    is_final_batch = _is_final_batch(plan, batch_id)

    task_run_summaries: list[PostBatchTaskRunSummary] = []
    executed_task_ids: list[int] = []
    successful_task_ids: list[int] = []
    problematic_run_ids: list[int] = []
    recovery_contexts: list[RecoveryContext] = []
    created_recovery_task_ids: list[int] = []

    next_batch_summary = _build_next_batch_summary(plan, batch_id)
    remaining_plan_summary = _build_remaining_plan_summary(plan, batch_id)

    for task_id in batch.task_ids:
        task = db.get(Task, task_id)
        if not task:
            raise PostBatchServiceError(
                f"Batch '{batch_id}' in plan version {plan.plan_version} references missing task {task_id}."
            )

        _require_task_is_ready_for_post_batch(
            task=task,
            batch_id=batch_id,
            plan_version=plan.plan_version,
        )

        latest_run = _require_terminal_run_for_task(
            db=db,
            task=task,
            batch_id=batch_id,
            plan_version=plan.plan_version,
        )

        latest_validation_artifact = _get_latest_validation_artifact_for_task(db, task.id)

        summary_failure_type = latest_run.failure_type
        summary_failure_code = latest_run.failure_code

        if task.status in {TASK_STATUS_FAILED, TASK_STATUS_PARTIAL} and latest_validation_artifact:
            summary_failure_type = summary_failure_type or "validation_decision"
            summary_failure_code = summary_failure_code or f"task_{task.status}"

        task_run_summaries.append(
            PostBatchTaskRunSummary(
                task_id=task.id,
                run_id=latest_run.id,
                run_status=f"{latest_run.status}|task:{task.status}",
                failure_type=summary_failure_type,
                failure_code=summary_failure_code,
            )
        )

        executed_task_ids.append(task.id)

        if task.status in {TASK_STATUS_FAILED, TASK_STATUS_PARTIAL}:
            validation_artifact = _require_validation_artifact_for_problematic_task(
                db=db,
                task=task,
                batch_id=batch_id,
                plan_version=plan.plan_version,
            )

            problematic_run_ids.append(latest_run.id)

            execution_context_summary = _build_execution_context_summary(
                task=task,
                latest_run=latest_run,
            )
            validation_context_summary = _build_validation_context_summary(
                task=task,
                validation_artifact=validation_artifact,
            )

            decision = generate_recovery_decision(
                db=db,
                run_id=latest_run.id,
                next_batch_summary=next_batch_summary,
                remaining_plan_summary=remaining_plan_summary,
                execution_context_summary=execution_context_summary,
                validation_context_summary=validation_context_summary,
            )
            persist_recovery_decision(
                db=db,
                project_id=project_id,
                decision=decision,
            )
            created_tasks = materialize_recovery_decision(
                db=db,
                project_id=project_id,
                decision=decision,
            )

            _require_recovery_source_task_remains_terminal(
                db=db,
                source_task_id=task.id,
                batch_id=batch_id,
                plan_version=plan.plan_version,
            )

            created_recovery_task_ids.extend(task.id for task in created_tasks)

            recovery_contexts.append(
                build_recovery_context_entry(
                    decision=decision,
                    created_tasks=created_tasks,
                )
            )

        elif task.status == TASK_STATUS_COMPLETED:
            successful_task_ids.append(task.id)

        else:
            raise PostBatchServiceError(
                f"Batch '{batch_id}' in plan version {plan.plan_version} reached an "
                f"unexpected terminal task status '{task.status}' for task {task.id}."
            )

    _reconcile_hierarchy_after_batch_changes(
        db=db,
        executed_task_ids=executed_task_ids,
        created_recovery_task_ids=created_recovery_task_ids,
    )

    aggregated_recovery_context = merge_recovery_contexts(recovery_contexts)

    if checkpoint_artifact_window_start_exclusive is not None:
        checkpoint_artifact_window_ids = _get_artifact_ids_in_checkpoint_window(
            db=db,
            project_id=project_id,
            start_exclusive=checkpoint_artifact_window_start_exclusive,
        )
    else:
        # Fallback defensivo para llamadas legacy o tests que no capturen todavía
        # el cutoff real al inicio del batch.
        checkpoint_artifact_window_ids = _get_artifact_ids_for_tasks(
            db=db,
            project_id=project_id,
            task_ids=executed_task_ids + created_recovery_task_ids,
        )

    evaluation_decision = evaluate_checkpoint(
        db=db,
        project_id=project_id,
        plan=plan,
        checkpoint_id=checkpoint.checkpoint_id,
        executed_task_ids_since_last_checkpoint=executed_task_ids,
        checkpoint_artifact_window_ids=checkpoint_artifact_window_ids,
        recovery_context=aggregated_recovery_context,
    )
    
    persist_evaluation_decision(
        db=db,
        project_id=project_id,
        decision=evaluation_decision,
    )

    resolved_action: str | None = None
    resolved_decision_signals: list[str] = []
    patched_execution_plan: ExecutionPlan | None = None

    if _is_new_stage_evaluation_output(evaluation_decision):
        current_batch_index = next(
            index
            for index, current_batch in enumerate(plan.execution_batches)
            if current_batch.batch_id == batch_id
        )
        remaining_batch_count = len(plan.execution_batches) - (current_batch_index + 1)

        preexisting_pending_valid_task_count = _count_preexisting_valid_pending_tasks(
            db=db,
            project_id=project_id,
            executed_task_ids=executed_task_ids,
            created_recovery_task_ids=created_recovery_task_ids,
        )

        new_recovery_pending_task_count = _count_valid_pending_tasks_for_ids(
            db=db,
            project_id=project_id,
            task_ids=created_recovery_task_ids,
        )

        has_pending_valid_tasks = (
            preexisting_pending_valid_task_count + new_recovery_pending_task_count
        ) > 0

        decision_signals = build_post_batch_decision_signals(
            evaluation_decision=evaluation_decision,
            recovery_context=aggregated_recovery_context,
            has_pending_valid_tasks=has_pending_valid_tasks,
            remaining_batch_count=remaining_batch_count,
            is_final_batch=is_final_batch,
        )

        # Enriquecimiento determinista local: distingue backlog previo vs trabajo nuevo recovery
        decision_signals.has_preexisting_pending_valid_tasks = (
            preexisting_pending_valid_task_count > 0
        )
        decision_signals.preexisting_pending_valid_task_count = (
            preexisting_pending_valid_task_count
        )
        decision_signals.has_new_recovery_pending_tasks = (
            new_recovery_pending_task_count > 0
        )
        decision_signals.new_recovery_pending_task_count = (
            new_recovery_pending_task_count
        )

        resolved = resolve_post_batch_decision(decision_signals)

        resolved_action = resolved.action
        resolved_decision_signals = list(getattr(decision_signals, "decision_signals", []) or [])

        normalized = NormalizedEvaluationOutcome(
            continue_execution=resolved.continue_execution,
            requires_replanning=resolved.requires_replanning,
            requires_resequencing=resolved.requires_resequencing,
            requires_manual_review=resolved.requires_manual_review,
            is_stage_closed=resolved.is_stage_closed,
            reopened_finalization=resolved.reopened_finalization,
            notes=resolved.notes,
        )
    else:
        normalized = _normalize_evaluation_outcome(evaluation_decision)

    normalized = _degrade_illegal_stage_closure_for_non_final_batch(
        normalized=normalized,
        is_final_batch=is_final_batch,
    )

    requires_replanning = normalized.requires_replanning
    requires_resequencing = normalized.requires_resequencing
    requires_manual_review = normalized.requires_manual_review
    continue_execution = normalized.continue_execution

    finalization_guard_triggered = False

    if continue_execution:
        status = "completed_with_evaluation"
    else:
        status = "checkpoint_blocked"

    notes = normalized.notes or "Post-batch processing completed with explicit checkpoint evaluation."

    if _should_materialize_patch_batch(
        normalized=normalized,
        evaluation_decision=evaluation_decision,
        created_recovery_task_ids=created_recovery_task_ids,
    ):
        patched_execution_plan = insert_patch_batch_after_batch(
            plan=plan,
            anchor_batch_id=batch.batch_id,
            task_ids=created_recovery_task_ids,
            goal="Execute recovery work required before continuing the pending plan.",
            checkpoint_reason=(
                "Validate the inserted recovery patch batch before continuing the remaining plan."
            ),
        )

        persist_patched_execution_plan(
            db=db,
            project_id=project_id,
            plan=patched_execution_plan,
        )

        notes = (
            f"{notes} A local patch batch was inserted into the current plan to execute "
            f"recovery-created work before continuing."
        )

    if resolved_action:
        signal_suffix = ""
        if resolved_decision_signals:
            signal_suffix = f" Signals: {', '.join(resolved_decision_signals)}."
        notes = f"[resolved_action={resolved_action}] {notes}{signal_suffix}"

    if is_final_batch:
        if normalized.is_stage_closed:
            continue_execution = False
            status = "project_stage_closed"
            notes = (
                normalized.notes
                or "The evaluator considered the final batch sufficient to close this project stage."
            )
        elif normalized.reopened_finalization or requires_replanning or requires_resequencing:
            next_finalization_iteration_count = finalization_iteration_count + 1

            if next_finalization_iteration_count > max_finalization_iterations:
                finalization_guard_triggered = True
                requires_manual_review = True
                continue_execution = False
                requires_resequencing = False
                requires_replanning = False
                status = "finalization_guard_blocked"
                notes = (
                    "Finalization guard triggered. The evaluator requested additional end-of-plan "
                    "work beyond the allowed automatic finalization iterations. Manual review is required."
                )
            else:
                finalization_iteration_count = next_finalization_iteration_count
                continue_execution = False
                status = "finalization_reopened"
                notes = (
                    normalized.notes
                    or "The evaluator reopened finalization. A new final iteration must be sequenced, "
                    "and the resulting plan must again end with an explicit final checkpoint."
                )
        else:
            if continue_execution:
                status = "completed_with_evaluation"
            else:
                status = "checkpoint_blocked"
            notes = normalized.notes or "Final batch evaluated but stage was not closed."

    result = PostBatchResult(
        project_id=project_id,
        plan_version=plan.plan_version,
        batch_id=batch.batch_id,
        checkpoint_id=checkpoint.checkpoint_id,
        status=status,
        executed_task_ids=executed_task_ids,
        successful_task_ids=successful_task_ids,
        problematic_run_ids=problematic_run_ids,
        task_run_summaries=task_run_summaries,
        recovery_context=aggregated_recovery_context,
        evaluation_decision=evaluation_decision,
        continue_execution=continue_execution,
        requires_resequencing=requires_resequencing,
        requires_replanning=requires_replanning,
        requires_manual_review=requires_manual_review,
        resolved_action=resolved_action,
        decision_signals_used=resolved_decision_signals,
        patched_execution_plan=patched_execution_plan,
        is_final_batch=is_final_batch,
        finalization_iteration_count=finalization_iteration_count,
        max_finalization_iterations=max_finalization_iterations,
        finalization_guard_triggered=finalization_guard_triggered,
        notes=notes,
    )

    workflow_iteration_trace = _build_workflow_iteration_trace(
        project_id=project_id,
        batch=batch,
        checkpoint_id=checkpoint.checkpoint_id,
        created_recovery_task_ids=created_recovery_task_ids,
        result=result,
    )

    if persist_result:
        _persist_post_batch_result(db=db, project_id=project_id, result=result)
        _persist_workflow_iteration_trace(
            db=db,
            project_id=project_id,
            trace=workflow_iteration_trace,
        )

    return result