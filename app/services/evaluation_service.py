import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.task import Task
from app.schemas.evaluation import RecoveryContext, StageEvaluationOutput
from app.schemas.execution_plan import ExecutionBatch, ExecutionPlan
from app.schemas.project_memory import ProjectOperationalContext
from app.services.artifacts import create_artifact
from app.services.evaluation_client import call_stage_evaluation_model
from app.services.project_memory_service import (
    build_and_persist_project_operational_context,
    build_project_operational_context,
    persist_project_operational_context,
)


CODE_VALIDATION_RESULT_ARTIFACT_TYPE = "code_validation_result"
EVALUATION_DECISION_ARTIFACT_TYPE = "evaluation_decision"


class EvaluationServiceError(Exception):
    """Base exception for evaluation service errors."""


def _serialize_evaluation_decision(decision: StageEvaluationOutput) -> str:
    return json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _safe_excerpt(value: str | None, limit: int = 2000) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _batch_to_dict(batch: ExecutionBatch) -> dict[str, Any]:
    return {
        "batch_id": batch.batch_id,
        "name": batch.name,
        "goal": batch.goal,
        "task_ids": batch.task_ids,
        "entry_conditions": batch.entry_conditions,
        "expected_outputs": batch.expected_outputs,
        "risk_level": batch.risk_level,
    }


def _get_checkpoint_or_raise(plan: ExecutionPlan, checkpoint_id: str):
    checkpoint = next((cp for cp in plan.checkpoints if cp.checkpoint_id == checkpoint_id), None)
    if not checkpoint:
        raise EvaluationServiceError(
            f"Checkpoint '{checkpoint_id}' not found in execution plan version {plan.plan_version}"
        )
    return checkpoint


def _get_checkpoint_batch_index_or_raise(plan: ExecutionPlan, checkpoint_id: str) -> int:
    checkpoint = _get_checkpoint_or_raise(plan, checkpoint_id)
    checkpoint_batch_index = next(
        (
            index
            for index, batch in enumerate(plan.execution_batches)
            if batch.batch_id == checkpoint.after_batch_id
        ),
        None,
    )
    if checkpoint_batch_index is None:
        raise EvaluationServiceError(
            f"Checkpoint '{checkpoint_id}' references unknown batch '{checkpoint.after_batch_id}'"
        )
    return checkpoint_batch_index


def _get_executed_tasks(
    db: Session,
    *,
    project_id: int,
    executed_task_ids_since_last_checkpoint: list[int],
) -> list[Task]:
    if not executed_task_ids_since_last_checkpoint:
        return []

    return (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.id.in_(executed_task_ids_since_last_checkpoint),
        )
        .order_by(Task.id.asc())
        .all()
    )


def _get_latest_run_for_task(db: Session, task_id: int) -> ExecutionRun | None:
    return (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )


def _get_task_artifacts(
    db: Session,
    *,
    project_id: int,
    task_id: int,
) -> list[Artifact]:
    return (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.task_id == task_id,
        )
        .order_by(Artifact.id.asc())
        .all()
    )


def _get_artifacts_since_last_checkpoint(
    db: Session,
    *,
    project_id: int,
    artifact_ids_since_last_checkpoint: list[int],
) -> list[Artifact]:
    if not artifact_ids_since_last_checkpoint:
        return []

    return (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.id.in_(artifact_ids_since_last_checkpoint),
        )
        .order_by(Artifact.id.asc())
        .all()
    )


def _artifact_to_summary_dict(artifact: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "artifact_type": artifact.artifact_type,
        "task_id": artifact.task_id,
        "content_excerpt": _safe_excerpt(artifact.content, limit=1200),
    }


def _task_to_stage_evidence(
    db: Session,
    *,
    project_id: int,
    task: Task,
) -> dict[str, Any]:
    latest_run = _get_latest_run_for_task(db, task.id)
    task_artifacts = _get_task_artifacts(
        db=db,
        project_id=project_id,
        task_id=task.id,
    )

    validation_artifact = next(
        (artifact for artifact in reversed(task_artifacts) if artifact.artifact_type == CODE_VALIDATION_RESULT_ARTIFACT_TYPE),
        None,
    )

    return {
        "task_id": task.id,
        "title": task.title,
        "status": task.status,
        "planning_level": task.planning_level,
        "task_type": task.task_type,
        "objective": task.objective,
        "acceptance_criteria": task.acceptance_criteria,
        "tests_required": task.tests_required,
        "latest_run": (
            {
                "run_id": latest_run.id,
                "status": latest_run.status,
                "failure_type": latest_run.failure_type,
                "failure_code": latest_run.failure_code,
                "work_summary": latest_run.work_summary,
                "work_details": _safe_excerpt(latest_run.work_details, limit=1500),
                "completed_scope": latest_run.completed_scope,
                "remaining_scope": latest_run.remaining_scope,
                "blockers_found": latest_run.blockers_found,
                "validation_notes": latest_run.validation_notes,
                "error_message": latest_run.error_message,
            }
            if latest_run
            else None
        ),
        "validation_artifact": (
            {
                "artifact_id": validation_artifact.id,
                "artifact_type": validation_artifact.artifact_type,
                "content_excerpt": _safe_excerpt(validation_artifact.content, limit=1800),
            }
            if validation_artifact
            else None
        ),
        "artifact_summaries": [_artifact_to_summary_dict(artifact) for artifact in task_artifacts],
    }


def _build_stage_goal(
    *,
    checkpoint: Any,
    plan: ExecutionPlan,
) -> str:
    parts: list[str] = []

    if getattr(checkpoint, "evaluation_goal", None):
        parts.append(f"Checkpoint evaluation goal: {checkpoint.evaluation_goal}")
    if getattr(checkpoint, "reason", None):
        parts.append(f"Checkpoint reason: {checkpoint.reason}")
    if getattr(checkpoint, "name", None):
        parts.append(f"Checkpoint name: {checkpoint.name}")
    if getattr(plan, "global_goal", None):
        parts.append(f"Plan global goal: {plan.global_goal}")

    return "\n".join(parts) if parts else "Evaluate whether the current stage can be safely closed or should continue."


def _build_stage_scope_summary(
    *,
    plan: ExecutionPlan,
    checkpoint_batch_index: int,
) -> str:
    current_batches = plan.execution_batches[: checkpoint_batch_index + 1]
    remaining_batches = plan.execution_batches[checkpoint_batch_index + 1 :]

    payload = {
        "plan_version": plan.plan_version,
        "sequencing_rationale": plan.sequencing_rationale,
        "blocked_task_ids": plan.blocked_task_ids,
        "batches_completed_or_evaluated_now": [_batch_to_dict(batch) for batch in current_batches],
        "remaining_batches": [_batch_to_dict(batch) for batch in remaining_batches],
        "uncertainties": plan.uncertainties,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_processed_batch_summary(
    *,
    batch: ExecutionBatch,
    executed_tasks: list[Task],
    artifacts_since_last_checkpoint: list[Artifact],
) -> str:
    task_ids = {task.id for task in executed_tasks}
    batch_tasks = [task for task in executed_tasks if task.id in batch.task_ids]
    batch_artifacts = [artifact for artifact in artifacts_since_last_checkpoint if artifact.task_id in task_ids]

    payload = {
        "evaluated_batch": _batch_to_dict(batch),
        "executed_task_count": len(batch_tasks),
        "executed_tasks": [
            {
                "task_id": task.id,
                "title": task.title,
                "status": task.status,
            }
            for task in batch_tasks
        ],
        "artifact_count_since_last_checkpoint": len(batch_artifacts),
        "artifact_types_since_last_checkpoint": [artifact.artifact_type for artifact in batch_artifacts],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_task_state_summary(
    db: Session,
    *,
    project_id: int,
    executed_tasks: list[Task],
) -> str:
    payload = {
        "evaluated_tasks": [
            _task_to_stage_evidence(
                db=db,
                project_id=project_id,
                task=task,
            )
            for task in executed_tasks
        ]
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_recovery_context_summary(recovery_context: RecoveryContext | None) -> str:
    resolved_recovery_context = recovery_context or RecoveryContext()

    payload = {
        "recovery_decisions": [
            decision.model_dump(mode="json")
            for decision in resolved_recovery_context.recovery_decisions
        ],
        "open_issues": [
            issue.model_dump(mode="json")
            for issue in resolved_recovery_context.open_issues
        ],
        "recovery_created_tasks": [
            created.model_dump(mode="json")
            for created in resolved_recovery_context.recovery_created_tasks
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_additional_context(
    *,
    project: Project,
    project_operational_context: ProjectOperationalContext,
    executed_tasks: list[Task],
    artifacts_since_last_checkpoint: list[Artifact],
    next_batch: ExecutionBatch | None,
) -> str:
    payload = {
        "project": {
            "project_id": project.id,
            "name": project.name,
            "goal_or_description": project.description or project.name,
        },
        "project_operational_context": project_operational_context.model_dump(mode="json"),
        "checkpoint_window": {
            "executed_task_ids": [task.id for task in executed_tasks],
            "artifact_ids": [artifact.id for artifact in artifacts_since_last_checkpoint],
        },
        "next_batch": _batch_to_dict(next_batch) if next_batch else None,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_stage_evaluation_request(
    db: Session,
    *,
    project_id: int,
    plan: ExecutionPlan,
    checkpoint_id: str,
    executed_task_ids_since_last_checkpoint: list[int],
    artifact_ids_since_last_checkpoint: list[int],
    recovery_context: RecoveryContext | None = None,
) -> dict[str, str]:
    project = db.get(Project, project_id)
    if not project:
        raise EvaluationServiceError(f"Project {project_id} not found")

    checkpoint = _get_checkpoint_or_raise(plan, checkpoint_id)
    checkpoint_batch_index = _get_checkpoint_batch_index_or_raise(plan, checkpoint_id)

    executed_tasks = _get_executed_tasks(
        db=db,
        project_id=project_id,
        executed_task_ids_since_last_checkpoint=executed_task_ids_since_last_checkpoint,
    )
    artifacts_since_last_checkpoint = _get_artifacts_since_last_checkpoint(
        db=db,
        project_id=project_id,
        artifact_ids_since_last_checkpoint=artifact_ids_since_last_checkpoint,
    )
    project_operational_context = build_project_operational_context(
        db=db,
        project_id=project_id,
    )

    current_batch = plan.execution_batches[checkpoint_batch_index]
    next_batch = (
        plan.execution_batches[checkpoint_batch_index + 1]
        if checkpoint_batch_index + 1 < len(plan.execution_batches)
        else None
    )

    return {
        "project_name": project.name,
        "project_description": project.description or project.name,
        "stage_goal": _build_stage_goal(
            checkpoint=checkpoint,
            plan=plan,
        ),
        "stage_scope_summary": _build_stage_scope_summary(
            plan=plan,
            checkpoint_batch_index=checkpoint_batch_index,
        ),
        "processed_batch_summary": _build_processed_batch_summary(
            batch=current_batch,
            executed_tasks=executed_tasks,
            artifacts_since_last_checkpoint=artifacts_since_last_checkpoint,
        ),
        "task_state_summary": _build_task_state_summary(
            db=db,
            project_id=project_id,
            executed_tasks=executed_tasks,
        ),
        "recovery_context_summary": _build_recovery_context_summary(recovery_context),
        "additional_context": _build_additional_context(
            project=project,
            project_operational_context=project_operational_context,
            executed_tasks=executed_tasks,
            artifacts_since_last_checkpoint=artifacts_since_last_checkpoint,
            next_batch=next_batch,
        ),
    }


def evaluate_checkpoint(
    db: Session,
    project_id: int,
    plan: ExecutionPlan,
    checkpoint_id: str,
    executed_task_ids_since_last_checkpoint: list[int],
    artifact_ids_since_last_checkpoint: list[int],
    recovery_context: RecoveryContext | None = None,
) -> StageEvaluationOutput:
    request = build_stage_evaluation_request(
        db=db,
        project_id=project_id,
        plan=plan,
        checkpoint_id=checkpoint_id,
        executed_task_ids_since_last_checkpoint=executed_task_ids_since_last_checkpoint,
        artifact_ids_since_last_checkpoint=artifact_ids_since_last_checkpoint,
        recovery_context=recovery_context,
    )

    return call_stage_evaluation_model(**request)


def persist_evaluation_decision(
    db: Session,
    project_id: int,
    decision: StageEvaluationOutput,
    created_by: str = "evaluation_agent",
) -> Artifact:
    project = db.get(Project, project_id)
    if not project:
        raise EvaluationServiceError(f"Project {project_id} not found")

    artifact = create_artifact(
        db=db,
        project_id=project_id,
        task_id=None,
        artifact_type=EVALUATION_DECISION_ARTIFACT_TYPE,
        content=_serialize_evaluation_decision(decision),
        created_by=created_by,
    )

    build_and_persist_project_operational_context(
        db=db,
        project_id=project_id,
        created_by="evaluation_service",
    )

    return artifact


def persist_project_operational_context_snapshot(
    db: Session,
    project_id: int,
    created_by: str = "evaluation_service",
) -> ProjectOperationalContext:
    """
    Explicit helper kept for callers that want to persist project memory
    independently of an evaluation decision artifact.
    """
    project_context = build_project_operational_context(
        db=db,
        project_id=project_id,
    )
    persist_project_operational_context(
        db=db,
        project_context=project_context,
        created_by=created_by,
    )
    return project_context