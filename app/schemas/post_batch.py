import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import (
    EXECUTION_RUN_STATUS_FAILED,
    EXECUTION_RUN_STATUS_PARTIAL,
    EXECUTION_RUN_STATUS_REJECTED,
    ExecutionRun,
)
from app.models.project import Project
from app.models.task import Task
from app.schemas.evaluation import RecoveryContext
from app.schemas.execution_plan import ExecutionBatch, ExecutionPlan
from app.schemas.post_batch import PostBatchResult, PostBatchTaskRunSummary
from app.services.artifacts import create_artifact
from app.services.evaluation_service import evaluate_checkpoint
from app.services.recovery_service import (
    build_recovery_context_entry,
    generate_recovery_decision,
    materialize_recovery_decision,
    merge_recovery_contexts,
    persist_recovery_decision,
)


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
    return next((cp for cp in plan.checkpoints if cp.after_batch_id == batch_id), None)


def _get_latest_run_for_task(db: Session, task_id: int) -> ExecutionRun | None:
    return (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )


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


def process_batch_after_execution(
    db: Session,
    project_id: int,
    plan: ExecutionPlan,
    batch_id: str,
    persist_result: bool = True,
) -> PostBatchResult:
    project = db.get(Project, project_id)
    if not project:
        raise PostBatchServiceError(f"Project {project_id} not found")

    batch = _get_batch(plan, batch_id)
    checkpoint = _get_checkpoint_for_batch(plan, batch_id)

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
            continue

        latest_run = _get_latest_run_for_task(db, task_id)

        task_run_summaries.append(
            PostBatchTaskRunSummary(
                task_id=task.id,
                run_id=latest_run.id if latest_run else None,
                run_status=latest_run.status if latest_run else None,
                failure_type=latest_run.failure_type if latest_run else None,
                failure_code=latest_run.failure_code if latest_run else None,
            )
        )

        if not latest_run:
            continue

        executed_task_ids.append(task.id)

        if latest_run.status in {
            EXECUTION_RUN_STATUS_REJECTED,
            EXECUTION_RUN_STATUS_PARTIAL,
            EXECUTION_RUN_STATUS_FAILED,
        }:
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
        else:
            successful_task_ids.append(task.id)

    aggregated_recovery_context = merge_recovery_contexts(recovery_contexts)

    if not checkpoint:
        result = PostBatchResult(
            project_id=project_id,
            plan_version=plan.plan_version,
            batch_id=batch.batch_id,
            checkpoint_id=None,
            status="completed_without_checkpoint",
            executed_task_ids=executed_task_ids,
            successful_task_ids=successful_task_ids,
            problematic_run_ids=problematic_run_ids,
            task_run_summaries=task_run_summaries,
            recovery_context=aggregated_recovery_context,
            evaluation_decision=None,
            continue_execution=len(aggregated_recovery_context.open_issues) == 0,
            requires_resequencing=False,
            requires_replanning=False,
            notes=(
                "Batch processed without a checkpoint. "
                "Continuation is tentatively allowed only if no open issues remain after recovery."
            ),
        )
        if persist_result:
            _persist_post_batch_result(db=db, project_id=project_id, result=result)
        return result

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

    requires_replanning = evaluation_decision.decision_type == "replan_from_level"
    requires_resequencing = (
        evaluation_decision.resequence_remaining_tasks
        or evaluation_decision.decision_type == "resequence_remaining_tasks"
        or evaluation_decision.decision_type == "insert_new_tasks"
    )

    status = (
        "completed_with_evaluation"
        if evaluation_decision.continue_execution
        else "checkpoint_blocked"
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
        continue_execution=evaluation_decision.continue_execution,
        requires_resequencing=requires_resequencing,
        requires_replanning=requires_replanning,
        notes=(
            "Post-batch processing completed with checkpoint evaluation. "
            "This result is tentative and may evolve once the executor lifecycle is refined."
        ),
    )

    if persist_result:
        _persist_post_batch_result(db=db, project_id=project_id, result=result)

    return result