# app/services/execution_runs.py

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import (
    EXECUTION_RUN_STATUS_FAILED,
    EXECUTION_RUN_STATUS_PARTIAL,
    EXECUTION_RUN_STATUS_PENDING,
    EXECUTION_RUN_STATUS_REJECTED,
    EXECUTION_RUN_STATUS_RUNNING,
    EXECUTION_RUN_STATUS_SUCCEEDED,
    FAILURE_TYPE_UNKNOWN,
    RECOVERY_ACTION_MANUAL_REVIEW,
    RECOVERY_ACTION_NONE,
    VALID_FAILURE_TYPES,
    VALID_RECOVERY_ACTIONS,
    ExecutionRun,
)
from app.models.task import TASK_STATUS_COMPLETED, Task

VALIDATION_RESULT_ARTIFACT_TYPE = "validation_result"


def _finalize_persistence(
    db: Session,
    *,
    entity,
    auto_commit: bool,
) -> None:
    if auto_commit:
        db.commit()
        db.refresh(entity)
    else:
        db.flush()


def _get_next_attempt_number(db: Session, task_id: int) -> int:
    current_max_attempt = db.scalar(
        select(func.max(ExecutionRun.attempt_number)).where(ExecutionRun.task_id == task_id)
    )
    if current_max_attempt is None:
        return 1
    return int(current_max_attempt) + 1


def _parse_artifact_json_content(artifact: Artifact) -> dict | None:
    try:
        payload = json.loads(artifact.content or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    return payload


def create_execution_run(
    db: Session,
    task_id: int,
    agent_name: str,
    input_snapshot: str | None = None,
    parent_run_id: int | None = None,
    auto_commit: bool = True,
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
    _finalize_persistence(db, entity=run, auto_commit=auto_commit)
    return run


def mark_execution_run_started(
    db: Session,
    run_id: int,
    auto_commit: bool = True,
) -> ExecutionRun | None:
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
    run.execution_agent_sequence = None
    run.artifacts_created = None
    run.completed_scope = None
    run.remaining_scope = None
    run.blockers_found = None
    run.validation_notes = None
    run.changed_files = None
    run.files_read = None
    run.change_dependencies = None

    _finalize_persistence(db, entity=run, auto_commit=auto_commit)
    return run


def mark_execution_run_succeeded(
    db: Session,
    run_id: int,
    output_snapshot: str | None = None,
    work_summary: str | None = None,
    work_details: str | None = None,
    execution_agent_sequence: str | None = None,
    artifacts_created: str | None = None,
    completed_scope: str | None = None,
    validation_notes: str | None = None,
    changed_files: str | None = None,
    files_read: str | None = None,
    change_dependencies: str | None = None,
    auto_commit: bool = True,
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
    run.execution_agent_sequence = execution_agent_sequence
    run.artifacts_created = artifacts_created
    run.completed_scope = completed_scope
    run.remaining_scope = None
    run.blockers_found = None
    run.validation_notes = validation_notes
    run.changed_files = changed_files
    run.files_read = files_read
    run.change_dependencies = change_dependencies

    _finalize_persistence(db, entity=run, auto_commit=auto_commit)
    return run


def mark_execution_run_partial(
    db: Session,
    run_id: int,
    output_snapshot: str | None = None,
    work_summary: str | None = None,
    work_details: str | None = None,
    execution_agent_sequence: str | None = None,
    artifacts_created: str | None = None,
    completed_scope: str | None = None,
    remaining_scope: str | None = None,
    blockers_found: str | None = None,
    validation_notes: str | None = None,
    changed_files: str | None = None,
    files_read: str | None = None,
    change_dependencies: str | None = None,
    recovery_action: str = RECOVERY_ACTION_MANUAL_REVIEW,
    auto_commit: bool = True,
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
    run.execution_agent_sequence = execution_agent_sequence
    run.artifacts_created = artifacts_created
    run.completed_scope = completed_scope
    run.remaining_scope = remaining_scope
    run.blockers_found = blockers_found
    run.validation_notes = validation_notes
    run.changed_files = changed_files
    run.files_read = files_read
    run.change_dependencies = change_dependencies

    _finalize_persistence(db, entity=run, auto_commit=auto_commit)
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
    execution_agent_sequence: str | None = None,
    artifacts_created: str | None = None,
    completed_scope: str | None = None,
    remaining_scope: str | None = None,
    blockers_found: str | None = None,
    validation_notes: str | None = None,
    changed_files: str | None = None,
    files_read: str | None = None,
    change_dependencies: str | None = None,
    auto_commit: bool = True,
) -> ExecutionRun | None:
    if failure_type not in VALID_FAILURE_TYPES:
        raise ValueError(
            f"Invalid failure_type '{failure_type}'. Allowed values: {sorted(VALID_FAILURE_TYPES)}"
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
    run.execution_agent_sequence = execution_agent_sequence
    run.artifacts_created = artifacts_created
    run.completed_scope = completed_scope
    run.remaining_scope = remaining_scope
    run.blockers_found = blockers_found
    run.validation_notes = validation_notes
    run.changed_files = changed_files
    run.files_read = files_read
    run.change_dependencies = change_dependencies

    _finalize_persistence(db, entity=run, auto_commit=auto_commit)
    return run


def mark_execution_run_rejected(
    db: Session,
    run_id: int,
    error_message: str,
    failure_code: str,
    recovery_action: str,
    work_summary: str | None = None,
    work_details: str | None = None,
    execution_agent_sequence: str | None = None,
    blockers_found: str | None = None,
    validation_notes: str | None = None,
    changed_files: str | None = None,
    files_read: str | None = None,
    change_dependencies: str | None = None,
    auto_commit: bool = True,
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
    run.execution_agent_sequence = execution_agent_sequence
    run.artifacts_created = None
    run.completed_scope = None
    run.remaining_scope = None
    run.blockers_found = blockers_found
    run.validation_notes = validation_notes
    run.changed_files = changed_files
    run.files_read = files_read
    run.change_dependencies = change_dependencies

    _finalize_persistence(db, entity=run, auto_commit=auto_commit)
    return run


def get_execution_run(db: Session, run_id: int) -> ExecutionRun | None:
    return db.get(ExecutionRun, run_id)


def get_completion_execution_run_for_task(
    db: Session,
    task_id: int,
) -> ExecutionRun | None:
    """
    Return the execution run that caused the task to reach `completed`.

    The source of truth is the validation_result artifact linked to the task:
    - artifact_type == "validation_result"
    - payload["final_task_status"] == "completed"
    - payload["execution_run_id"] identifies the canonical run

    If the task is not currently completed, or no consistent completion artifact exists,
    return None.
    """
    task = db.get(Task, task_id)
    if not task:
        return None

    if task.status != TASK_STATUS_COMPLETED:
        return None

    completion_artifacts = (
        db.query(Artifact)
        .filter(
            Artifact.task_id == task_id,
            Artifact.artifact_type == VALIDATION_RESULT_ARTIFACT_TYPE,
        )
        .order_by(Artifact.id.asc())
        .all()
    )

    completion_run_id: int | None = None

    for artifact in completion_artifacts:
        payload = _parse_artifact_json_content(artifact)
        if not payload:
            continue

        if payload.get("task_id") != task_id:
            continue

        if payload.get("final_task_status") != TASK_STATUS_COMPLETED:
            continue

        artifact_run_id = payload.get("execution_run_id")
        if not isinstance(artifact_run_id, int):
            continue

        completion_run_id = artifact_run_id

    if completion_run_id is None:
        return None

    run = db.get(ExecutionRun, completion_run_id)
    if not run:
        return None

    if run.task_id != task_id:
        return None

    return run
