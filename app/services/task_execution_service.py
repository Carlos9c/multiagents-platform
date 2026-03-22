from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.execution_run import (
    FAILURE_TYPE_INTERNAL,
    RECOVERY_ACTION_MANUAL_REVIEW,
    RECOVERY_ACTION_REATOMIZE,
    ExecutionRun,
)
from app.models.task import (
    CODE_EXECUTOR,
    EXECUTABLE_TASK_STATUSES,
    PLANNING_LEVEL_ATOMIC,
    PENDING_ATOMIC_ASSIGNMENT_EXECUTOR,
    Task,
)
from app.services.execution_runs import (
    create_execution_run,
    get_execution_run,
    mark_execution_run_failed,
    mark_execution_run_partial,
    mark_execution_run_rejected,
    mark_execution_run_started,
    mark_execution_run_succeeded,
)
from app.services.executor import (
    ExecutorInternalError,
    ExecutorRejectedError,
    execute_atomic_task,
)
from app.services.tasks import (
    mark_task_awaiting_validation,
    mark_task_running,
)

SUPPORTED_EXECUTORS = {
    CODE_EXECUTOR,
}


class TaskExecutionServiceError(Exception):
    """Base exception for task execution orchestration errors."""


@dataclass
class AsyncTaskExecutionStartResult:
    task_id: int
    execution_run_id: int
    celery_task_id: str
    executor_type: str
    message: str = "Execution started"


@dataclass
class SyncTaskExecutionResult:
    task_id: int
    execution_run_id: int
    run_status: str
    executor_type: str
    output_snapshot: str | None
    message: str


def _get_task_or_raise(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise TaskExecutionServiceError(f"Task {task_id} not found")
    return task


def _validate_task_is_executable(task: Task) -> None:
    if task.is_blocked:
        raise TaskExecutionServiceError(
            f"Task is blocked: {task.blocking_reason or 'unknown reason'}"
        )

    if task.planning_level != PLANNING_LEVEL_ATOMIC:
        raise TaskExecutionServiceError(
            "Only atomic tasks can be executed. "
            "Executor assignment must be resolved during the atomic stage."
        )

    if not task.executor_type or task.executor_type == PENDING_ATOMIC_ASSIGNMENT_EXECUTOR:
        raise TaskExecutionServiceError(
            "Task executor is not assigned yet. "
            "Atomic task generation must assign a concrete executor before execution."
        )

    if task.executor_type not in SUPPORTED_EXECUTORS:
        raise TaskExecutionServiceError(
            f"Unsupported executor_type '{task.executor_type}'. "
            f"Supported executors: {sorted(SUPPORTED_EXECUTORS)}"
        )

    if task.status not in EXECUTABLE_TASK_STATUSES:
        raise TaskExecutionServiceError(
            f"Task status '{task.status}' is not executable. "
            f"Allowed statuses: {sorted(EXECUTABLE_TASK_STATUSES)}"
        )


def _create_execution_run_for_task(db: Session, task: Task) -> ExecutionRun:
    return create_execution_run(
        db=db,
        task_id=task.id,
        agent_name="executor_agent",
        input_snapshot=f"Executing task {task.id}: {task.title}",
    )


def execute_existing_run_sync(db: Session, run_id: int) -> SyncTaskExecutionResult:
    run = get_execution_run(db, run_id)
    if not run:
        raise TaskExecutionServiceError(f"ExecutionRun {run_id} not found")

    task = db.get(Task, run.task_id)
    if not task:
        raise TaskExecutionServiceError(f"Task {run.task_id} not found")

    try:
        _validate_task_is_executable(task)

        mark_execution_run_started(db, run_id)
        mark_task_running(db, task.id)

        result = execute_atomic_task(db=db, task=task)

        if result.status == "succeeded":
            mark_execution_run_succeeded(
                db=db,
                run_id=run_id,
                output_snapshot=result.output_snapshot,
                work_summary=result.work_summary,
                work_details=result.work_details,
                artifacts_created=result.artifacts_created,
                completed_scope=result.completed_scope,
                validation_notes=result.validation_notes,
            )
            mark_task_awaiting_validation(db, task.id)

            return SyncTaskExecutionResult(
                task_id=task.id,
                execution_run_id=run_id,
                run_status="succeeded",
                executor_type=task.executor_type,
                output_snapshot=result.output_snapshot,
                message="Execution finished synchronously and is awaiting validation.",
            )

        if result.status == "partial":
            mark_execution_run_partial(
                db=db,
                run_id=run_id,
                output_snapshot=result.output_snapshot,
                work_summary=result.work_summary,
                work_details=result.work_details,
                artifacts_created=result.artifacts_created,
                completed_scope=result.completed_scope,
                remaining_scope=result.remaining_scope,
                blockers_found=result.blockers_found,
                validation_notes=result.validation_notes,
                recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
            )
            mark_task_awaiting_validation(db, task.id)

            return SyncTaskExecutionResult(
                task_id=task.id,
                execution_run_id=run_id,
                run_status="partial",
                executor_type=task.executor_type,
                output_snapshot=result.output_snapshot,
                message="Execution finished synchronously and is awaiting validation.",
            )

        raise TaskExecutionServiceError(
            f"Unsupported executor result status '{result.status}' returned by executor."
        )

    except ExecutorRejectedError as exc:
        mark_execution_run_rejected(
            db=db,
            run_id=run_id,
            error_message=exc.message,
            failure_code=exc.failure_code,
            recovery_action=RECOVERY_ACTION_REATOMIZE,
            work_summary=exc.work_summary,
            work_details=exc.work_details,
            blockers_found=exc.blockers_found,
            validation_notes=exc.validation_notes,
        )
        mark_task_awaiting_validation(db, task.id)

        return SyncTaskExecutionResult(
            task_id=task.id,
            execution_run_id=run_id,
            run_status="rejected",
            executor_type=task.executor_type,
            output_snapshot=None,
            message="Execution finished synchronously and is awaiting validation.",
        )

    except ExecutorInternalError as exc:
        mark_execution_run_failed(
            db=db,
            run_id=run_id,
            error_message=exc.message,
            failure_type=FAILURE_TYPE_INTERNAL,
            failure_code=exc.failure_code,
            recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
        )
        mark_task_awaiting_validation(db, task.id)

        return SyncTaskExecutionResult(
            task_id=task.id,
            execution_run_id=run_id,
            run_status="failed",
            executor_type=task.executor_type,
            output_snapshot=None,
            message="Execution finished synchronously and is awaiting validation.",
        )

    except Exception as exc:
        mark_execution_run_failed(
            db=db,
            run_id=run_id,
            error_message=str(exc),
            failure_type=FAILURE_TYPE_INTERNAL,
            failure_code="task_execution_service_error",
            recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
        )
        mark_task_awaiting_validation(db, task.id)

        return SyncTaskExecutionResult(
            task_id=task.id,
            execution_run_id=run_id,
            run_status="failed",
            executor_type=task.executor_type,
            output_snapshot=None,
            message="Execution finished synchronously and is awaiting validation.",
        )


def execute_task_sync(db: Session, task_id: int) -> SyncTaskExecutionResult:
    task = _get_task_or_raise(db, task_id)
    _validate_task_is_executable(task)

    execution_run = _create_execution_run_for_task(db, task)
    return execute_existing_run_sync(db=db, run_id=execution_run.id)


def start_task_execution_async(db: Session, task_id: int) -> AsyncTaskExecutionStartResult:
    task = _get_task_or_raise(db, task_id)
    _validate_task_is_executable(task)

    execution_run = _create_execution_run_for_task(db, task)

    from app.workers.tasks import execute_task as execute_task_job

    async_result = execute_task_job.delay(execution_run.id)

    return AsyncTaskExecutionStartResult(
        task_id=task.id,
        execution_run_id=execution_run.id,
        celery_task_id=async_result.id,
        executor_type=task.executor_type,
    )