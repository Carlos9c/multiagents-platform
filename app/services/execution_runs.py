from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.execution_run import (
    EXECUTION_RUN_STATUS_FAILED,
    EXECUTION_RUN_STATUS_PARTIAL,
    EXECUTION_RUN_STATUS_PENDING,
    EXECUTION_RUN_STATUS_REJECTED,
    EXECUTION_RUN_STATUS_RUNNING,
    EXECUTION_RUN_STATUS_SUCCEEDED,
    FAILURE_TYPE_INTERNAL,
    FAILURE_TYPE_UNKNOWN,
    RECOVERY_ACTION_MANUAL_REVIEW,
    RECOVERY_ACTION_NONE,
    VALID_EXECUTION_RUN_STATUSES,
    VALID_FAILURE_TYPES,
    VALID_RECOVERY_ACTIONS,
    ExecutionRun,
)


def _get_next_attempt_number(db: Session, task_id: int) -> int:
    current_max_attempt = db.scalar(
        select(func.max(ExecutionRun.attempt_number)).where(ExecutionRun.task_id == task_id)
    )
    if current_max_attempt is None:
        return 1
    return int(current_max_attempt) + 1


def create_execution_run(
    db: Session,
    task_id: int,
    agent_name: str,
    input_snapshot: str | None = None,
    parent_run_id: int | None = None,
) -> ExecutionRun:
    run = ExecutionRun(
        task_id=task_id,
        parent_run_id=parent_run_id,
        agent_name=agent_name,
        attempt_number=_get_next_attempt_number(db, task_id),
        status=EXECUTION_RUN_STATUS_PENDING,
        input_snapshot=input_snapshot,
        recovery_action=RECOVERY_ACTION_NONE,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def mark_execution_run_started(db: Session, run_id: int) -> ExecutionRun | None:
    run = db.get(ExecutionRun, run_id)
    if not run:
        return None

    run.status = EXECUTION_RUN_STATUS_RUNNING
    run.error_message = None
    run.failure_type = None
    run.failure_code = None
    run.recovery_action = RECOVERY_ACTION_NONE
    run.output_snapshot = None
    run.work_summary = None
    run.work_details = None
    run.artifacts_created = None
    run.completed_scope = None
    run.remaining_scope = None
    run.blockers_found = None
    run.validation_notes = None

    db.commit()
    db.refresh(run)
    return run


def mark_execution_run_succeeded(
    db: Session,
    run_id: int,
    output_snapshot: str | None = None,
    work_summary: str | None = None,
    work_details: str | None = None,
    artifacts_created: str | None = None,
    completed_scope: str | None = None,
    validation_notes: str | None = None,
) -> ExecutionRun | None:
    run = db.get(ExecutionRun, run_id)
    if not run:
        return None

    run.status = EXECUTION_RUN_STATUS_SUCCEEDED
    run.output_snapshot = output_snapshot
    run.error_message = None
    run.failure_type = None
    run.failure_code = None
    run.recovery_action = RECOVERY_ACTION_NONE
    run.work_summary = work_summary
    run.work_details = work_details
    run.artifacts_created = artifacts_created
    run.completed_scope = completed_scope
    run.remaining_scope = None
    run.blockers_found = None
    run.validation_notes = validation_notes

    db.commit()
    db.refresh(run)
    return run


def mark_execution_run_partial(
    db: Session,
    run_id: int,
    output_snapshot: str | None = None,
    work_summary: str | None = None,
    work_details: str | None = None,
    artifacts_created: str | None = None,
    completed_scope: str | None = None,
    remaining_scope: str | None = None,
    blockers_found: str | None = None,
    validation_notes: str | None = None,
    recovery_action: str = RECOVERY_ACTION_MANUAL_REVIEW,
) -> ExecutionRun | None:
    if recovery_action not in VALID_RECOVERY_ACTIONS:
        raise ValueError(
            f"Invalid recovery_action '{recovery_action}'. "
            f"Allowed values: {sorted(VALID_RECOVERY_ACTIONS)}"
        )

    run = db.get(ExecutionRun, run_id)
    if not run:
        return None

    run.status = EXECUTION_RUN_STATUS_PARTIAL
    run.output_snapshot = output_snapshot
    run.error_message = None
    run.failure_type = None
    run.failure_code = None
    run.recovery_action = recovery_action
    run.work_summary = work_summary
    run.work_details = work_details
    run.artifacts_created = artifacts_created
    run.completed_scope = completed_scope
    run.remaining_scope = remaining_scope
    run.blockers_found = blockers_found
    run.validation_notes = validation_notes

    db.commit()
    db.refresh(run)
    return run


def mark_execution_run_failed(
    db: Session,
    run_id: int,
    error_message: str,
    failure_type: str = FAILURE_TYPE_UNKNOWN,
    failure_code: str | None = None,
    recovery_action: str = RECOVERY_ACTION_MANUAL_REVIEW,
    work_summary: str | None = None,
    work_details: str | None = None,
    artifacts_created: str | None = None,
    completed_scope: str | None = None,
    remaining_scope: str | None = None,
    blockers_found: str | None = None,
    validation_notes: str | None = None,
) -> ExecutionRun | None:
    if failure_type not in VALID_FAILURE_TYPES:
        raise ValueError(
            f"Invalid failure_type '{failure_type}'. "
            f"Allowed values: {sorted(VALID_FAILURE_TYPES)}"
        )

    if recovery_action not in VALID_RECOVERY_ACTIONS:
        raise ValueError(
            f"Invalid recovery_action '{recovery_action}'. "
            f"Allowed values: {sorted(VALID_RECOVERY_ACTIONS)}"
        )

    run = db.get(ExecutionRun, run_id)
    if not run:
        return None

    run.status = EXECUTION_RUN_STATUS_FAILED
    run.error_message = error_message
    run.failure_type = failure_type
    run.failure_code = failure_code
    run.recovery_action = recovery_action
    run.work_summary = work_summary
    run.work_details = work_details
    run.artifacts_created = artifacts_created
    run.completed_scope = completed_scope
    run.remaining_scope = remaining_scope
    run.blockers_found = blockers_found
    run.validation_notes = validation_notes

    db.commit()
    db.refresh(run)
    return run


def mark_execution_run_rejected(
    db: Session,
    run_id: int,
    error_message: str,
    failure_code: str,
    recovery_action: str,
    work_summary: str | None = None,
    work_details: str | None = None,
    blockers_found: str | None = None,
    validation_notes: str | None = None,
) -> ExecutionRun | None:
    if recovery_action not in VALID_RECOVERY_ACTIONS:
        raise ValueError(
            f"Invalid recovery_action '{recovery_action}'. "
            f"Allowed values: {sorted(VALID_RECOVERY_ACTIONS)}"
        )

    run = db.get(ExecutionRun, run_id)
    if not run:
        return None

    run.status = EXECUTION_RUN_STATUS_REJECTED
    run.error_message = error_message
    run.failure_type = "executor_rejected"
    run.failure_code = failure_code
    run.recovery_action = recovery_action
    run.work_summary = work_summary
    run.work_details = work_details
    run.artifacts_created = None
    run.completed_scope = None
    run.remaining_scope = None
    run.blockers_found = blockers_found
    run.validation_notes = validation_notes

    db.commit()
    db.refresh(run)
    return run


def set_execution_run_internal_error(
    db: Session,
    run_id: int,
    error_message: str,
    failure_code: str = "internal_executor_error",
) -> ExecutionRun | None:
    return mark_execution_run_failed(
        db=db,
        run_id=run_id,
        error_message=error_message,
        failure_type=FAILURE_TYPE_INTERNAL,
        failure_code=failure_code,
        recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
        validation_notes="The executor failed due to an internal unexpected error.",
    )


def get_execution_run(db: Session, run_id: int) -> ExecutionRun | None:
    return db.get(ExecutionRun, run_id)


def validate_execution_run_status(value: str) -> str:
    if value not in VALID_EXECUTION_RUN_STATUSES:
        raise ValueError(
            f"Invalid execution run status '{value}'. "
            f"Allowed values: {sorted(VALID_EXECUTION_RUN_STATUSES)}"
        )
    return value