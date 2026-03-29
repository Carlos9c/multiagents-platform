import json
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
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.schemas.execution_plan import ExecutionBatch, ExecutionPlan
from app.schemas.post_batch import PostBatchResult, PostBatchTaskRunSummary
from app.schemas.post_batch_intent import ResolvedPostBatchIntent
from app.schemas.recovery import RecoveryContext
from app.schemas.recovery_assignment import (
    AssignmentEvaluationSignals,
    AssignmentRecoverySignal,
    AssignmentRecoverySignals,
    ExecutedBatchAssignmentSummary,
    KnownAssignmentRelationships,
    LivePlanSummaryForAssignment,
    NextUsefulProgressSummary,
    PendingTaskSummary,
    RecoveryAssignmentInput,
    RecoveryTaskForAssignment,
    RemainingBatchSummary,
)
from app.schemas.workflow_iteration_trace import WorkflowIterationTrace
from app.services.artifacts import create_artifact
from app.services.evaluation_service import (
    evaluate_checkpoint,
    persist_evaluation_decision,
)
from app.services.live_plan_mutation_service import (
    LivePlanMutationServiceError,
    mutate_live_plan,
)
from app.services.post_batch_decision_service import (
    build_post_batch_decision_signals,
    resolve_post_batch_intent,
)
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

VALIDATION_RESULT_ARTIFACT_TYPE = "validation_result"


class PostBatchServiceError(Exception):
    """Base exception for post-batch orchestration errors."""


def _serialize_workflow_iteration_trace(trace: WorkflowIterationTrace) -> str:
    return json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _build_workflow_iteration_trace(
    *,
    project_id: int,
    batch: ExecutionBatch,
    checkpoint_id: str,
    created_recovery_task_ids: list[int],
    result: PostBatchResult,
    assigned_task_ids: list[int],
    unassigned_task_ids: list[int],
    source_run_ids_with_recovery: list[int],
    preexisting_pending_valid_task_count: int,
    new_recovery_pending_task_count: int,
) -> WorkflowIterationTrace:
    return WorkflowIterationTrace(
        project_id=project_id,
        plan_version=result.plan_version,
        batch_internal_id=batch.batch_internal_id,
        batch_id=batch.batch_id,
        batch_index=batch.batch_index,
        checkpoint_id=checkpoint_id,
        post_batch_status=result.status,
        executed_task_ids=list(result.executed_task_ids),
        successful_task_ids=list(result.successful_task_ids),
        problematic_run_ids=list(result.problematic_run_ids),
        created_recovery_task_ids=list(created_recovery_task_ids),
        source_run_ids_with_recovery=list(source_run_ids_with_recovery),
        resolved_intent_type=result.resolved_intent_type,
        resolved_mutation_scope=result.resolved_mutation_scope,
        remaining_plan_still_valid=result.remaining_plan_still_valid,
        has_new_recovery_tasks=result.has_new_recovery_tasks,
        requires_plan_mutation=result.requires_plan_mutation,
        requires_all_new_tasks_assigned=result.requires_all_new_tasks_assigned,
        can_continue_after_application=result.can_continue_after_application,
        should_close_stage=result.should_close_stage,
        requires_manual_review=result.requires_manual_review,
        reopened_finalization=result.reopened_finalization,
        decision_signals=list(result.decision_signals),
        patched_plan_version=(
            result.patched_execution_plan.plan_version
            if result.patched_execution_plan is not None
            else None
        ),
        assigned_task_ids=list(assigned_task_ids),
        unassigned_task_ids=list(unassigned_task_ids),
        preexisting_pending_valid_task_count=preexisting_pending_valid_task_count,
        new_recovery_pending_task_count=new_recovery_pending_task_count,
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


def _serialize_json_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _persist_recovery_assignment_payload(
    db: Session,
    *,
    project_id: int,
    artifact_type: str,
    payload: dict,
    created_by: str = "post_batch_processor",
) -> Artifact:
    return create_artifact(
        db=db,
        project_id=project_id,
        task_id=None,
        artifact_type=artifact_type,
        content=_serialize_json_payload(payload),
        created_by=created_by,
    )


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


def _parse_validation_artifact_payload(
    validation_artifact: Artifact,
) -> dict[str, Any]:
    try:
        parsed = json.loads(validation_artifact.content)
    except json.JSONDecodeError:
        return {
            "artifact_id": validation_artifact.id,
            "artifact_type": validation_artifact.artifact_type,
            "raw_content": validation_artifact.content,
            "parse_error": "validation artifact content is not valid JSON",
        }

    if not isinstance(parsed, dict):
        return {
            "artifact_id": validation_artifact.id,
            "artifact_type": validation_artifact.artifact_type,
            "raw_content": validation_artifact.content,
            "parse_error": "validation artifact content is not a JSON object",
        }

    return parsed


def _build_recovery_oriented_validation_summary(
    *,
    validation_artifact: Artifact,
) -> dict[str, Any]:
    payload = _parse_validation_artifact_payload(validation_artifact)

    return {
        "artifact_id": validation_artifact.id,
        "artifact_type": validation_artifact.artifact_type,
        "execution_run_id": payload.get("execution_run_id"),
        "validator_key": payload.get("validator_key"),
        "discipline": payload.get("discipline"),
        "validation_mode": payload.get("validation_mode"),
        "decision": payload.get("decision"),
        "summary": payload.get("summary"),
        "validated_scope": payload.get("validated_scope"),
        "missing_scope": payload.get("missing_scope"),
        "blockers": payload.get("blockers") or [],
        "manual_review_required": bool(payload.get("manual_review_required", False)),
        "followup_validation_required": bool(payload.get("followup_validation_required", False)),
        "final_task_status": payload.get("final_task_status"),
        "raw_validation_artifact_content": validation_artifact.content,
        "parse_error": payload.get("parse_error"),
    }


def _get_latest_validation_artifact_for_task(
    db: Session,
    task_id: int,
) -> Artifact | None:
    return (
        db.query(Artifact)
        .filter(
            Artifact.task_id == task_id,
            Artifact.artifact_type == VALIDATION_RESULT_ARTIFACT_TYPE,
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
            f"task {task.id} is '{task.status}' but has no '{VALIDATION_RESULT_ARTIFACT_TYPE}' artifact."
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
        "validation_summary_for_recovery": _build_recovery_oriented_validation_summary(
            validation_artifact=validation_artifact,
        ),
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


def _validate_stage_evaluation_coherence(
    *,
    evaluation_decision: Any,
) -> None:
    recommended_next_action = _normalize_string(
        _read_attr(evaluation_decision, "recommended_next_action"),
        "",
    )
    remaining_plan_still_valid = _normalize_bool(
        _read_attr(evaluation_decision, "remaining_plan_still_valid"),
        True,
    )
    replan = _read_attr(evaluation_decision, "replan", None)
    replan_required = _normalize_bool(_read_attr(replan, "required"), False)
    replan_level = _normalize_string(_read_attr(replan, "level"), "")

    if recommended_next_action == "replan_remaining_work" and remaining_plan_still_valid:
        raise PostBatchServiceError(
            "Checkpoint evaluation is contradictory: recommended_next_action='replan_remaining_work' "
            "requires remaining_plan_still_valid=False."
        )

    if replan_required and replan_level == "high_level" and remaining_plan_still_valid:
        raise PostBatchServiceError(
            "Checkpoint evaluation is contradictory: high-level replanning requires "
            "remaining_plan_still_valid=False."
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


def _require_new_stage_evaluation_output(evaluation_decision: Any) -> None:
    if not _is_new_stage_evaluation_output(evaluation_decision):
        raise PostBatchServiceError(
            "post_batch_service no longer supports legacy checkpoint evaluation outputs. "
            "evaluate_checkpoint() must return the current StageEvaluationOutput contract."
        )


def _normalize_non_final_close_intent(
    *,
    intent: ResolvedPostBatchIntent,
    is_final_batch: bool,
) -> ResolvedPostBatchIntent:
    if is_final_batch or intent.intent_type != "close":
        return intent

    decision_signals = list(dict.fromkeys(intent.decision_signals + ["non_final_close_degraded"]))

    return ResolvedPostBatchIntent(
        intent_type="continue",
        mutation_scope="none",
        remaining_plan_still_valid=intent.remaining_plan_still_valid,
        has_new_recovery_tasks=False,
        requires_plan_mutation=False,
        requires_all_new_tasks_assigned=False,
        can_continue_after_application=True,
        should_close_stage=False,
        requires_manual_review=False,
        reopened_finalization=False,
        notes=(
            "Evaluator proposed closing the stage at a non-final batch. "
            "Stage closure was deferred because only the final batch can close the stage in the current workflow."
        ),
        decision_signals=decision_signals,
    )


def _build_recovery_assignment_executed_batch_summary(
    *,
    batch: ExecutionBatch,
    executed_task_ids: list[int],
    successful_task_ids: list[int],
    problematic_run_ids: list[int],
    task_run_summaries: list[PostBatchTaskRunSummary],
) -> ExecutedBatchAssignmentSummary:
    partial_or_failed_task_ids = [
        summary.task_id
        for summary in task_run_summaries
        if summary.task_id in executed_task_ids and summary.task_id not in successful_task_ids
    ]

    key_findings: list[str] = []
    if successful_task_ids:
        key_findings.append(
            f"Successful tasks in current batch: {', '.join(str(task_id) for task_id in successful_task_ids)}."
        )
    if problematic_run_ids:
        key_findings.append(
            f"Problematic execution runs detected: {', '.join(str(run_id) for run_id in problematic_run_ids)}."
        )

    summary = (
        f"Batch '{batch.batch_id}' finished execution and checkpoint evaluation is assigning "
        "new recovery work into the live plan before the next batch starts."
    )

    return ExecutedBatchAssignmentSummary(
        batch_id=batch.batch_id,
        batch_name=batch.name,
        goal=batch.goal,
        executed_task_ids=list(executed_task_ids),
        completed_task_ids=list(successful_task_ids),
        partial_task_ids=list(partial_or_failed_task_ids),
        failed_task_ids=[],
        summary=summary,
        key_findings=key_findings,
    )


def _build_recovery_assignment_recovery_signals(
    recovery_context: RecoveryContext,
) -> AssignmentRecoverySignals:
    entries: list[AssignmentRecoverySignal] = []
    for decision in recovery_context.recovery_decisions:
        entries.append(
            AssignmentRecoverySignal(
                source_task_id=decision.source_task_id,
                source_run_id=decision.source_run_id,
                recovery_action=decision.action,
                recovery_reason=decision.reason,
                covered_gap_summary=decision.reason,
                still_blocks_progress=decision.still_blocks_progress,
                execution_guidance=None,
                evaluation_guidance=None,
            )
        )
    return AssignmentRecoverySignals(entries=entries)


def _get_tasks_by_ids(
    db: Session,
    *,
    project_id: int,
    task_ids: list[int],
) -> list[Task]:
    if not task_ids:
        return []

    tasks = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.id.in_(task_ids),
        )
        .all()
    )
    by_id = {task.id: task for task in tasks}
    missing = [task_id for task_id in task_ids if task_id not in by_id]
    if missing:
        raise PostBatchServiceError(
            f"Recovery assignment could not find tasks in project {project_id}: {missing}"
        )
    return [by_id[task_id] for task_id in task_ids]


def _build_recovery_assignment_new_tasks(
    db: Session,
    *,
    project_id: int,
    created_recovery_task_ids: list[int],
    recovery_context: RecoveryContext,
) -> list[RecoveryTaskForAssignment]:
    created_task_records_by_id = {
        record.created_task_id: record for record in recovery_context.recovery_created_tasks
    }
    tasks = _get_tasks_by_ids(
        db=db,
        project_id=project_id,
        task_ids=created_recovery_task_ids,
    )

    parent_ids = [task.parent_task_id for task in tasks if task.parent_task_id is not None]
    parent_titles: dict[int, str] = {}
    if parent_ids:
        parent_tasks = _get_tasks_by_ids(
            db=db,
            project_id=project_id,
            task_ids=list(dict.fromkeys(parent_ids)),
        )
        parent_titles = {task.id: task.title for task in parent_tasks}

    output: list[RecoveryTaskForAssignment] = []
    for task in tasks:
        record = created_task_records_by_id.get(task.id)
        output.append(
            RecoveryTaskForAssignment(
                task_id=task.id,
                title=task.title,
                description=task.description or task.summary or task.title,
                objective=task.objective,
                implementation_notes=task.implementation_notes,
                acceptance_criteria=task.acceptance_criteria,
                technical_constraints=task.technical_constraints,
                out_of_scope=task.out_of_scope,
                task_type=task.task_type,
                priority=task.priority,
                parent_task_id=task.parent_task_id,
                parent_task_title=(
                    parent_titles.get(task.parent_task_id) if task.parent_task_id else None
                ),
                sequence_order=task.sequence_order,
                source_task_id=record.source_task_id if record else None,
                source_run_id=record.source_run_id if record else None,
            )
        )
    return output


def _build_recovery_assignment_live_plan_summary(
    *,
    plan: ExecutionPlan,
    batch: ExecutionBatch,
) -> LivePlanSummaryForAssignment:
    current_index = next(
        index
        for index, current_batch in enumerate(plan.execution_batches)
        if current_batch.batch_id == batch.batch_id
    )
    remaining_batches = plan.execution_batches[current_index + 1 :]

    return LivePlanSummaryForAssignment(
        plan_version=plan.plan_version,
        current_batch_id=batch.batch_id,
        current_batch_name=batch.name,
        remaining_batches=[
            RemainingBatchSummary(
                batch_id=item.batch_id,
                batch_name=item.name,
                batch_index=item.batch_index,
                goal=item.goal,
                task_ids=list(item.task_ids),
                task_titles=[str(task_id) for task_id in item.task_ids],
                checkpoint_reason=item.checkpoint_reason,
                is_patch_batch=item.is_patch_batch,
            )
            for item in remaining_batches
        ],
    )


def _build_recovery_assignment_next_useful_progress(
    *,
    plan: ExecutionPlan,
    batch: ExecutionBatch,
) -> NextUsefulProgressSummary | None:
    current_index = next(
        index
        for index, current_batch in enumerate(plan.execution_batches)
        if current_batch.batch_id == batch.batch_id
    )
    if current_index + 1 >= len(plan.execution_batches):
        return None

    next_batch = plan.execution_batches[current_index + 1]
    return NextUsefulProgressSummary(
        summary=(
            f"The next useful progress is the next pending execution batch '{next_batch.name}'."
        ),
        task_ids=list(next_batch.task_ids),
        batch_id=next_batch.batch_id,
        batch_name=next_batch.name,
    )


def _build_recovery_assignment_pending_valid_tasks(
    db: Session,
    *,
    project_id: int,
    exclude_task_ids: list[int],
) -> list[PendingTaskSummary]:
    tasks = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.status == TASK_STATUS_PENDING,
            Task.is_blocked.is_(False),
            ~Task.id.in_(exclude_task_ids) if exclude_task_ids else True,
        )
        .order_by(Task.id.asc())
        .all()
    )

    parent_ids = [task.parent_task_id for task in tasks if task.parent_task_id is not None]
    parent_titles: dict[int, str] = {}
    if parent_ids:
        parent_tasks = (
            db.query(Task)
            .filter(
                Task.project_id == project_id,
                Task.id.in_(list(dict.fromkeys(parent_ids))),
            )
            .all()
        )
        parent_titles = {task.id: task.title for task in parent_tasks}

    return [
        PendingTaskSummary(
            task_id=task.id,
            title=task.title,
            parent_task_id=task.parent_task_id,
            parent_task_title=(
                parent_titles.get(task.parent_task_id) if task.parent_task_id else None
            ),
            status=task.status,
            is_blocked=bool(task.is_blocked),
            sequence_order=task.sequence_order,
        )
        for task in tasks
    ]


def _build_recovery_assignment_input(
    db: Session,
    *,
    project: Project,
    plan: ExecutionPlan,
    batch: ExecutionBatch,
    evaluation_decision: Any,
    recovery_context: RecoveryContext,
    created_recovery_task_ids: list[int],
    executed_task_ids: list[int],
    successful_task_ids: list[int],
    problematic_run_ids: list[int],
    task_run_summaries: list[PostBatchTaskRunSummary],
    resolved_intent_type: str,
    resolved_mutation_scope: str,
) -> RecoveryAssignmentInput:
    new_tasks = _build_recovery_assignment_new_tasks(
        db=db,
        project_id=project.id,
        created_recovery_task_ids=created_recovery_task_ids,
        recovery_context=recovery_context,
    )

    exclude_ids = list(dict.fromkeys(executed_task_ids + created_recovery_task_ids))

    return RecoveryAssignmentInput(
        project_id=project.id,
        project_goal=(
            getattr(project, "goal", None)
            or getattr(project, "objective", None)
            or getattr(project, "name", None)
            or "Continue the current project safely."
        ),
        current_stage_summary=getattr(project, "description", None),
        resolved_intent_type=resolved_intent_type,
        resolved_mutation_scope=resolved_mutation_scope,
        executed_batch_summary=_build_recovery_assignment_executed_batch_summary(
            batch=batch,
            executed_task_ids=executed_task_ids,
            successful_task_ids=successful_task_ids,
            problematic_run_ids=problematic_run_ids,
            task_run_summaries=task_run_summaries,
        ),
        evaluation_signals=AssignmentEvaluationSignals(
            decision=_normalize_string(
                _read_attr(evaluation_decision, "decision"), "stage_incomplete"
            ),
            decision_summary=_normalize_string(
                _read_attr(evaluation_decision, "decision_summary"),
                "Checkpoint evaluation decided that the current plan can continue with controlled assignment.",
            ),
            recommended_next_action=_normalize_string(
                _read_attr(evaluation_decision, "recommended_next_action"),
                resolved_intent_type,
            ),
            recommended_next_action_reason=_normalize_string(
                _read_attr(evaluation_decision, "recommended_next_action_reason"),
                "New recovery work must be assigned before the next batch starts.",
            ),
            plan_change_scope=_read_attr(evaluation_decision, "plan_change_scope", "none"),
            remaining_plan_still_valid=_normalize_bool(
                _read_attr(evaluation_decision, "remaining_plan_still_valid"),
                True,
            ),
            new_recovery_tasks_blocking=_read_attr(
                evaluation_decision, "new_recovery_tasks_blocking"
            ),
            single_task_tail_risk=_normalize_bool(
                _read_attr(evaluation_decision, "single_task_tail_risk"),
                False,
            ),
            decision_signals=list(_read_attr(evaluation_decision, "decision_signals", []) or []),
            key_risks=list(_read_attr(evaluation_decision, "key_risks", []) or []),
            notes=list(_read_attr(evaluation_decision, "notes", []) or []),
        ),
        recovery_signals=_build_recovery_assignment_recovery_signals(recovery_context),
        new_tasks=new_tasks,
        live_plan_summary=_build_recovery_assignment_live_plan_summary(
            plan=plan,
            batch=batch,
        ),
        next_useful_progress=_build_recovery_assignment_next_useful_progress(
            plan=plan,
            batch=batch,
        ),
        pending_valid_tasks=_build_recovery_assignment_pending_valid_tasks(
            db=db,
            project_id=project.id,
            exclude_task_ids=exclude_ids,
        ),
        known_relationships=KnownAssignmentRelationships(),
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

    _require_new_stage_evaluation_output(evaluation_decision)
    _validate_stage_evaluation_coherence(evaluation_decision=evaluation_decision)

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

    decision_signals.has_preexisting_pending_valid_tasks = preexisting_pending_valid_task_count > 0
    decision_signals.preexisting_pending_valid_task_count = preexisting_pending_valid_task_count
    decision_signals.has_new_recovery_pending_tasks = new_recovery_pending_task_count > 0
    decision_signals.new_recovery_pending_task_count = new_recovery_pending_task_count

    resolved_intent = resolve_post_batch_intent(decision_signals)
    resolved_intent = _normalize_non_final_close_intent(
        intent=resolved_intent,
        is_final_batch=is_final_batch,
    )

    patched_execution_plan: ExecutionPlan | None = None
    finalization_guard_triggered = False
    assigned_task_ids: list[int] = []
    unassigned_task_ids: list[int] = []
    source_run_ids_with_recovery = [
        decision.source_run_id for decision in aggregated_recovery_context.recovery_decisions
    ]

    notes = (
        resolved_intent.notes
        or "Post-batch processing completed with explicit checkpoint evaluation."
    )

    if resolved_intent.intent_type in {"assign", "resequence"}:
        try:
            mutation_result = mutate_live_plan(
                db=db,
                project=project,
                plan=plan,
                batch=batch,
                resolved_intent=resolved_intent,
                evaluation_decision=evaluation_decision,
                recovery_context=aggregated_recovery_context,
                created_recovery_task_ids=created_recovery_task_ids,
                executed_task_ids=executed_task_ids,
                successful_task_ids=successful_task_ids,
                problematic_run_ids=problematic_run_ids,
                task_run_summaries=task_run_summaries,
                build_recovery_assignment_input_fn=_build_recovery_assignment_input,
                persist_recovery_assignment_payload_fn=_persist_recovery_assignment_payload,
            )
        except LivePlanMutationServiceError as exc:
            raise PostBatchServiceError(f"Live plan mutation failed: {str(exc)}") from exc

        assigned_task_ids = list(mutation_result.metadata.get("assigned_task_ids", []))
        unassigned_task_ids = list(mutation_result.metadata.get("unassigned_task_ids", []))

        if mutation_result.mutation_kind == "assignment":
            patched_execution_plan = mutation_result.patched_execution_plan
            cluster_assignments = mutation_result.metadata.get(
                "compiled_cluster_assignments",
                [],
            )
            cluster_count = len(cluster_assignments)

            notes = (
                f"{notes} Recovery assignment placed all new tasks before continuing. "
                f"clusters_assigned={cluster_count}; assigned_task_ids={assigned_task_ids}."
            )

        elif mutation_result.mutation_kind == "escalated_to_replan":
            patched_execution_plan = None
            resolved_intent = ResolvedPostBatchIntent(
                intent_type="replan",
                mutation_scope="replan",
                remaining_plan_still_valid=False,
                has_new_recovery_tasks=resolved_intent.has_new_recovery_tasks,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=True,
                notes=(
                    f"{notes} Recovery assignment escalated to replanning because the newly created "
                    f"work revealed a structural conflict: "
                    f"{' '.join(mutation_result.notes).strip() or 'no extra notes'}"
                ),
                decision_signals=list(
                    dict.fromkeys(
                        list(resolved_intent.decision_signals) + ["assignment_escalated_to_replan"]
                    )
                ),
            )
            notes = resolved_intent.notes

        elif mutation_result.mutation_kind == "resequence_patch":
            patched_execution_plan = mutation_result.patched_execution_plan
            notes = (
                f"{notes} "
                "A local patch batch was inserted into the current plan to execute recovery-created work before continuing."
            )

        elif mutation_result.mutation_kind == "resequence_deferred":
            patched_execution_plan = None
            notes = (
                f"{notes} "
                "The remaining plan requires resequencing, but no immediate local patch batch was materialized."
            )

        else:
            raise PostBatchServiceError(
                f"Unsupported mutation result '{mutation_result.mutation_kind}' for intent '{resolved_intent.intent_type}'."
            )

    if (
        resolved_intent.requires_all_new_tasks_assigned
        and created_recovery_task_ids
        and patched_execution_plan is None
        and resolved_intent.intent_type != "replan"
    ):
        raise PostBatchServiceError(
            "Post-batch intent required assignment of all new recovery tasks, "
            "but no patched execution plan was produced."
        )

    if is_final_batch:
        if (
            patched_execution_plan is not None
            and resolved_intent.can_continue_after_application
            and resolved_intent.intent_type == "assign"
        ):
            status = "finalization_reopened"
            resolved_intent = ResolvedPostBatchIntent(
                intent_type=resolved_intent.intent_type,
                mutation_scope=resolved_intent.mutation_scope,
                remaining_plan_still_valid=resolved_intent.remaining_plan_still_valid,
                has_new_recovery_tasks=resolved_intent.has_new_recovery_tasks,
                requires_plan_mutation=resolved_intent.requires_plan_mutation,
                requires_all_new_tasks_assigned=resolved_intent.requires_all_new_tasks_assigned,
                can_continue_after_application=resolved_intent.can_continue_after_application,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=True,
                notes=(
                    f"{notes} The original final batch no longer closes the stage because new work "
                    "was assigned into the live plan. The stage remains open until the new final batch is evaluated."
                ),
                decision_signals=list(resolved_intent.decision_signals),
            )
            notes = resolved_intent.notes

        elif resolved_intent.intent_type == "close":
            status = "project_stage_closed"
            notes = (
                resolved_intent.notes
                or "The evaluator considered the final batch sufficient to close this project stage."
            )

        elif resolved_intent.reopened_finalization:
            next_finalization_iteration_count = finalization_iteration_count + 1

            if next_finalization_iteration_count > max_finalization_iterations:
                finalization_guard_triggered = True
                resolved_intent = ResolvedPostBatchIntent(
                    intent_type="manual_review",
                    mutation_scope="none",
                    remaining_plan_still_valid=resolved_intent.remaining_plan_still_valid,
                    has_new_recovery_tasks=resolved_intent.has_new_recovery_tasks,
                    requires_plan_mutation=False,
                    requires_all_new_tasks_assigned=False,
                    can_continue_after_application=False,
                    should_close_stage=False,
                    requires_manual_review=True,
                    reopened_finalization=False,
                    notes=(
                        "Finalization guard triggered. The evaluator requested additional end-of-plan "
                        "work beyond the allowed automatic finalization iterations. Manual review is required."
                    ),
                    decision_signals=list(
                        dict.fromkeys(
                            list(resolved_intent.decision_signals)
                            + ["finalization_guard_triggered"]
                        )
                    ),
                )
                status = "finalization_guard_blocked"
                notes = resolved_intent.notes
            else:
                finalization_iteration_count = next_finalization_iteration_count
                status = "finalization_reopened"
                notes = (
                    resolved_intent.notes
                    or "The evaluator reopened finalization. A new final iteration must be sequenced, "
                    "and the resulting plan must again end with an explicit final checkpoint."
                )

        elif resolved_intent.intent_type == "continue":
            status = "completed_with_evaluation"
        elif resolved_intent.intent_type == "manual_review":
            status = "checkpoint_blocked"
        elif resolved_intent.intent_type in {"assign", "resequence", "replan"}:
            status = "checkpoint_blocked"
        else:
            raise PostBatchServiceError(
                f"Unsupported resolved intent_type '{resolved_intent.intent_type}'."
            )

    else:
        if resolved_intent.intent_type == "continue":
            status = "completed_with_evaluation"
        elif resolved_intent.intent_type == "assign":
            status = "completed_with_evaluation"
        elif resolved_intent.intent_type in {
            "resequence",
            "replan",
            "manual_review",
            "close",
        }:
            status = (
                "checkpoint_blocked"
                if resolved_intent.intent_type != "close"
                else "project_stage_closed"
            )
        else:
            raise PostBatchServiceError(
                f"Unsupported resolved intent_type '{resolved_intent.intent_type}'."
            )

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
        resolved_intent_type=resolved_intent.intent_type,
        resolved_mutation_scope=resolved_intent.mutation_scope,
        remaining_plan_still_valid=resolved_intent.remaining_plan_still_valid,
        has_new_recovery_tasks=resolved_intent.has_new_recovery_tasks,
        requires_plan_mutation=resolved_intent.requires_plan_mutation,
        requires_all_new_tasks_assigned=resolved_intent.requires_all_new_tasks_assigned,
        can_continue_after_application=resolved_intent.can_continue_after_application,
        should_close_stage=resolved_intent.should_close_stage,
        requires_manual_review=resolved_intent.requires_manual_review,
        reopened_finalization=resolved_intent.reopened_finalization,
        decision_signals=list(resolved_intent.decision_signals),
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
        assigned_task_ids=assigned_task_ids,
        unassigned_task_ids=unassigned_task_ids,
        source_run_ids_with_recovery=source_run_ids_with_recovery,
        preexisting_pending_valid_task_count=preexisting_pending_valid_task_count,
        new_recovery_pending_task_count=new_recovery_pending_task_count,
    )

    if persist_result:
        _persist_post_batch_result(db=db, project_id=project_id, result=result)
        _persist_workflow_iteration_trace(
            db=db,
            project_id=project_id,
            trace=workflow_iteration_trace,
        )

    return result
