import json

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
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.schemas.evaluation import RecoveryContext
from app.schemas.execution_plan import ExecutionBatch, ExecutionPlan
from app.schemas.post_batch import PostBatchResult, PostBatchTaskRunSummary
from app.services.artifacts import create_artifact
from app.services.evaluation_service import evaluate_checkpoint, persist_evaluation_decision
from app.services.recovery_service import (
    build_recovery_context_entry,
    generate_recovery_decision,
    materialize_recovery_decision,
    merge_recovery_contexts,
    persist_recovery_decision,
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


class PostBatchServiceError(Exception):
    """Base exception for post-batch orchestration errors."""


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


def _is_final_batch(plan: ExecutionPlan, batch_id: str) -> bool:
    if not plan.execution_batches:
        return False
    return plan.execution_batches[-1].batch_id == batch_id


def _requires_plan_change_from_evaluation(evaluation_decision) -> bool:
    return (
        evaluation_decision.decision_type in {
            "insert_new_tasks",
            "resequence_remaining_tasks",
            "replan_from_level",
        }
        or evaluation_decision.resequence_remaining_tasks
    )


def process_batch_after_execution(
    db: Session,
    project_id: int,
    plan: ExecutionPlan,
    batch_id: str,
    persist_result: bool = True,
    finalization_iteration_count: int = 0,
    max_finalization_iterations: int = 2,
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

        task_run_summaries.append(
            PostBatchTaskRunSummary(
                task_id=task.id,
                run_id=latest_run.id,
                run_status=latest_run.status,
                failure_type=latest_run.failure_type,
                failure_code=latest_run.failure_code,
            )
        )

        executed_task_ids.append(task.id)

        if task.status in {TASK_STATUS_FAILED, TASK_STATUS_PARTIAL}:
            problematic_run_ids.append(latest_run.id)

            decision = generate_recovery_decision(
                db=db,
                run_id=latest_run.id,
                next_batch_summary=next_batch_summary,
                remaining_plan_summary=remaining_plan_summary,
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

    aggregated_recovery_context = merge_recovery_contexts(recovery_contexts)

    artifact_ids_since_last_checkpoint = _get_artifact_ids_for_tasks(
        db=db,
        project_id=project_id,
        task_ids=executed_task_ids,
    )

    evaluation_decision = evaluate_checkpoint(
        db=db,
        project_id=project_id,
        plan=plan,
        checkpoint_id=checkpoint.checkpoint_id,
        executed_task_ids_since_last_checkpoint=executed_task_ids,
        artifact_ids_since_last_checkpoint=artifact_ids_since_last_checkpoint,
        recovery_context=aggregated_recovery_context,
    )

    persist_evaluation_decision(
        db=db,
        project_id=project_id,
        decision=evaluation_decision,
    )

    requires_replanning = evaluation_decision.decision_type == "replan_from_level"
    requires_resequencing = _requires_plan_change_from_evaluation(evaluation_decision)

    finalization_guard_triggered = False
    requires_manual_review = evaluation_decision.decision_type == "manual_review"
    continue_execution = evaluation_decision.continue_execution
    status = "completed_with_evaluation" if continue_execution else "checkpoint_blocked"
    notes = "Post-batch processing completed with explicit checkpoint evaluation."

    if is_final_batch:
        if _requires_plan_change_from_evaluation(evaluation_decision):
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
                    "The evaluator reopened finalization. A new final iteration must be sequenced, "
                    "and the resulting plan must again end with an explicit final checkpoint."
                )
        else:
            continue_execution = False
            status = "project_stage_closed"
            notes = (
                "The evaluator considered the final batch sufficient to close this project stage."
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
        continue_execution=continue_execution,
        requires_resequencing=requires_resequencing,
        requires_replanning=requires_replanning,
        requires_manual_review=requires_manual_review,
        is_final_batch=is_final_batch,
        finalization_iteration_count=finalization_iteration_count,
        max_finalization_iterations=max_finalization_iterations,
        finalization_guard_triggered=finalization_guard_triggered,
        notes=notes,
    )

    if persist_result:
        _persist_post_batch_result(db=db, project_id=project_id, result=result)

    return result