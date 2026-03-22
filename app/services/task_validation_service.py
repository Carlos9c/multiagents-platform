from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.task import (
    TASK_STATUS_AWAITING_VALIDATION,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    Task,
)
from app.schemas.code_execution import CodeExecutorResult
from app.schemas.code_validation import (
    CODE_VALIDATION_DECIDED_STATUS_COMPLETED,
    CODE_VALIDATION_DECIDED_STATUS_FAILED,
    CODE_VALIDATION_DECIDED_STATUS_PARTIAL,
    CodeValidationResult,
)
from app.services.code_validator import LocalCodeValidator
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_storage import CODE_DOMAIN, ProjectStorageService
from app.services.workspace_runtime import WorkspaceRuntimeError


class TaskValidationServiceError(Exception):
    """Base exception for task validation orchestration."""


@dataclass
class TaskValidationServiceResult:
    task_id: int
    execution_run_id: int
    final_task_status: str
    message: str
    validation_result: CodeValidationResult


def _get_task_or_raise(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise TaskValidationServiceError(f"Task {task_id} not found.")
    return task


def _mark_task_completed(db: Session, task_id: int) -> Task:
    task = _get_task_or_raise(db, task_id)
    task.status = TASK_STATUS_COMPLETED
    db.commit()
    db.refresh(task)
    return task


def _mark_task_partial(db: Session, task_id: int) -> Task:
    task = _get_task_or_raise(db, task_id)
    task.status = TASK_STATUS_PARTIAL
    db.commit()
    db.refresh(task)
    return task


def _mark_task_failed(db: Session, task_id: int) -> Task:
    task = _get_task_or_raise(db, task_id)
    task.status = TASK_STATUS_FAILED
    db.commit()
    db.refresh(task)
    return task


def _promote_completed_workspace_to_source(
    *,
    db: Session,
    task: Task,
    execution_run_id: int,
) -> None:
    """
    Promotes the validated workspace into the canonical code source baseline.

    This must happen only for validated completed tasks.
    If promotion fails, the task must not be treated as successfully completed,
    because future tasks would otherwise execute against an outdated source baseline.
    """
    runtime = LocalWorkspaceRuntime(
        storage_service=ProjectStorageService()
    )

    try:
        runtime.promote_workspace_to_source(
            project_id=task.project_id,
            execution_run_id=execution_run_id,
            domain_name=CODE_DOMAIN,
        )
    except WorkspaceRuntimeError as exc:
        raise TaskValidationServiceError(
            f"Task {task.id} was validated as completed, but promotion to source failed: {str(exc)}"
        ) from exc
    except Exception as exc:
        raise TaskValidationServiceError(
            f"Unexpected error while promoting validated workspace for task {task.id}: {str(exc)}"
        ) from exc


def validate_code_task(
    db: Session,
    task_id: int,
    execution_run_id: int,
    executor_result: CodeExecutorResult,
) -> TaskValidationServiceResult:
    task = _get_task_or_raise(db, task_id)

    if task.status != TASK_STATUS_AWAITING_VALIDATION:
        raise TaskValidationServiceError(
            f"Task {task.id} is not awaiting validation. Current status='{task.status}'."
        )

    validator = LocalCodeValidator(db=db)
    validation_result = validator.validate(
        task=task,
        execution_run_id=execution_run_id,
        executor_result=executor_result,
    )

    decided_status = validation_result.decided_task_status

    if decided_status == CODE_VALIDATION_DECIDED_STATUS_COMPLETED:
        try:
            _promote_completed_workspace_to_source(
                db=db,
                task=task,
                execution_run_id=execution_run_id,
            )
        except TaskValidationServiceError:
            _mark_task_failed(db, task.id)
            raise

        _mark_task_completed(db, task.id)
        return TaskValidationServiceResult(
            task_id=task.id,
            execution_run_id=execution_run_id,
            final_task_status=TASK_STATUS_COMPLETED,
            message=(
                "Task validation completed successfully, the validated workspace was promoted "
                "to source, and the task is now completed."
            ),
            validation_result=validation_result,
        )

    if decided_status == CODE_VALIDATION_DECIDED_STATUS_PARTIAL:
        _mark_task_partial(db, task.id)
        return TaskValidationServiceResult(
            task_id=task.id,
            execution_run_id=execution_run_id,
            final_task_status=TASK_STATUS_PARTIAL,
            message="Task validation finished with a partial result. No source promotion was performed.",
            validation_result=validation_result,
        )

    if decided_status == CODE_VALIDATION_DECIDED_STATUS_FAILED:
        _mark_task_failed(db, task.id)
        return TaskValidationServiceResult(
            task_id=task.id,
            execution_run_id=execution_run_id,
            final_task_status=TASK_STATUS_FAILED,
            message="Task validation failed. No source promotion was performed.",
            validation_result=validation_result,
        )

    raise TaskValidationServiceError(
        f"Unsupported validation decided_task_status '{decided_status}'."
    )