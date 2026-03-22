import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_PENDING,
    Task,
)
from app.schemas.evaluation import RecoveryContext
from app.services.artifacts import create_artifact
from app.services.recovery_client import (
    RECOVERY_ACTION_INSERT_FOLLOWUP,
    RECOVERY_ACTION_MANUAL_REVIEW,
    RECOVERY_ACTION_REATOMIZE,
    RECOVERY_ACTION_RETRY,
    RecoveryClientError,
    RecoveryDecision,
    evaluate_recovery_decision,
)


class RecoveryServiceError(Exception):
    """Base exception for recovery service errors."""


def _serialize_recovery_decision(decision: RecoveryDecision) -> str:
    return json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _get_run_or_raise(db: Session, run_id: int) -> ExecutionRun:
    run = db.get(ExecutionRun, run_id)
    if not run:
        raise RecoveryServiceError(f"ExecutionRun {run_id} not found")
    return run


def _get_task_or_raise(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise RecoveryServiceError(f"Task {task_id} not found")
    return task


def generate_recovery_decision(
    db: Session,
    run_id: int,
    next_batch_summary: str | None = None,
    remaining_plan_summary: str | None = None,
    execution_context_summary: str | None = None,
    validation_context_summary: str | None = None,
) -> RecoveryDecision:
    run = _get_run_or_raise(db, run_id)
    task = _get_task_or_raise(db, run.task_id)

    if not execution_context_summary:
        raise RecoveryServiceError(
            f"Recovery decision for run {run_id} requires execution_context_summary."
        )

    if not validation_context_summary:
        raise RecoveryServiceError(
            f"Recovery decision for run {run_id} requires validation_context_summary."
        )

    try:
        return evaluate_recovery_decision(
            task=task,
            run=run,
            next_batch_summary=next_batch_summary,
            remaining_plan_summary=remaining_plan_summary,
            execution_context_summary=execution_context_summary,
            validation_context_summary=validation_context_summary,
        )
    except RecoveryClientError as exc:
        raise RecoveryServiceError(
            f"Recovery model failed for run {run_id}: {str(exc)}"
        ) from exc


def persist_recovery_decision(
    db: Session,
    project_id: int,
    decision: RecoveryDecision,
    created_by: str = "recovery_service",
) -> Artifact:
    return create_artifact(
        db=db,
        project_id=project_id,
        task_id=None,
        artifact_type="recovery_decision",
        content=_serialize_recovery_decision(decision),
        created_by=created_by,
    )


def materialize_recovery_decision(
    db: Session,
    project_id: int,
    decision: RecoveryDecision,
    parent_task_id: int | None = None,
) -> list[Task]:
    created_tasks: list[Task] = []

    if decision.action == RECOVERY_ACTION_RETRY:
        return created_tasks

    if decision.action == RECOVERY_ACTION_MANUAL_REVIEW:
        return created_tasks

    if decision.action in {RECOVERY_ACTION_REATOMIZE, RECOVERY_ACTION_INSERT_FOLLOWUP}:
        for item in decision.created_tasks:
            task = Task(
                project_id=project_id,
                parent_task_id=parent_task_id,
                title=item.title,
                description=item.description,
                objective=item.objective,
                implementation_notes=item.implementation_notes,
                acceptance_criteria=item.acceptance_criteria,
                technical_constraints=item.technical_constraints,
                out_of_scope=item.out_of_scope,
                priority=item.priority,
                task_type=item.task_type,
                planning_level=PLANNING_LEVEL_ATOMIC,
                executor_type=item.executor_type,
                status=TASK_STATUS_PENDING,
            )
            db.add(task)
            created_tasks.append(task)

        db.commit()

        for task in created_tasks:
            db.refresh(task)

        return created_tasks

    raise RecoveryServiceError(
        f"Recovery decision action '{decision.action}' is not materializable."
    )


def build_recovery_context_entry(
    decision: RecoveryDecision,
    created_tasks: list[Task],
) -> RecoveryContext:
    created_task_ids = [task.id for task in created_tasks]

    return RecoveryContext(
        recovery_summary=decision.reason,
        decisions_taken=[
            f"action={decision.action}",
            f"retry_same_task={decision.retry_same_task}",
            f"requires_manual_review={decision.requires_manual_review}",
        ],
        inserted_tasks=created_task_ids,
        unresolved_failures=[],
    )


def merge_recovery_contexts(contexts: list[RecoveryContext]) -> RecoveryContext | None:
    if not contexts:
        return None

    recovery_summaries: list[str] = []
    decisions_taken: list[str] = []
    inserted_tasks: list[int] = []
    unresolved_failures: list[str] = []

    for context in contexts:
        if context.recovery_summary:
            recovery_summaries.append(context.recovery_summary)

        decisions_taken.extend(context.decisions_taken)
        inserted_tasks.extend(context.inserted_tasks)
        unresolved_failures.extend(context.unresolved_failures)

    return RecoveryContext(
        recovery_summary=" | ".join(recovery_summaries) if recovery_summaries else None,
        decisions_taken=decisions_taken,
        inserted_tasks=inserted_tasks,
        unresolved_failures=unresolved_failures,
    )