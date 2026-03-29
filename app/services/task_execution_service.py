from dataclasses import dataclass
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.execution_engine import (
    ExecutionEngineError,
    ExecutionEngineRejectedError,
    get_execution_engine,
)
from app.execution_engine.contracts import (
    EXECUTION_DECISION_COMPLETED,
    EXECUTION_DECISION_FAILED,
    EXECUTION_DECISION_PARTIAL,
    EXECUTION_DECISION_REJECTED,
)
from app.execution_engine.request_adapter import build_execution_request
from app.models.artifact import Artifact
from app.models.execution_run import (
    FAILURE_TYPE_INTERNAL,
    RECOVERY_ACTION_MANUAL_REVIEW,
    RECOVERY_ACTION_REATOMIZE,
    ExecutionRun,
)
from app.models.task import (
    EXECUTION_ENGINE,
    EXECUTABLE_TASK_STATUSES,
    PLANNING_LEVEL_ATOMIC,
    PENDING_ENGINE_ROUTING_EXECUTOR,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    Task,
)
from app.services.artifacts import create_artifact
from app.services.execution_runs import (
    create_execution_run,
    get_execution_run,
    mark_execution_run_failed,
    mark_execution_run_rejected,
    mark_execution_run_started,
    mark_execution_run_succeeded,
)
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_storage import CODE_DOMAIN, ProjectStorageService
from app.services.task_hierarchy_reconciliation_service import (
    TaskHierarchyReconciliationServiceError,
    reconcile_task_hierarchy_after_changes,
)
from app.services.tasks import (
    mark_task_failed,
    mark_task_running,
)
from app.services.validation.service import (
    ValidationServiceError,
    ValidationServiceResult,
    validate_execution_result,
)

logger = logging.getLogger(__name__)


SUPPORTED_EXECUTORS = {EXECUTION_ENGINE}
VALIDATION_RESULT_ARTIFACT_TYPE = "validation_result"
VALIDATION_RESULT_ARTIFACT_CREATED_BY = "task_execution_service"


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
    final_task_status: str | None = None
    validation_decision: str | None = None


def _get_task_or_raise(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise TaskExecutionServiceError(f"Task {task_id} not found")
    return task


def _get_execution_run_or_raise(db: Session, run_id: int) -> ExecutionRun:
    run = get_execution_run(db, run_id)
    if not run:
        raise TaskExecutionServiceError(f"ExecutionRun {run_id} not found")
    return run


def _validate_task_is_executable(task: Task) -> None:
    if task.is_blocked:
        raise TaskExecutionServiceError(
            f"Task is blocked: {task.blocking_reason or 'unknown reason'}"
        )

    if task.planning_level != PLANNING_LEVEL_ATOMIC:
        raise TaskExecutionServiceError("Only atomic tasks can be executed.")

    if task.status not in EXECUTABLE_TASK_STATUSES:
        raise TaskExecutionServiceError(
            f"Task status '{task.status}' is not executable. "
            f"Allowed statuses: {sorted(EXECUTABLE_TASK_STATUSES)}"
        )


def _resolve_executor_type_for_task(task: Task) -> str:
    if task.planning_level != PLANNING_LEVEL_ATOMIC:
        raise TaskExecutionServiceError("Only atomic tasks can be executed.")

    executor_type = task.executor_type

    if not executor_type or executor_type == PENDING_ENGINE_ROUTING_EXECUTOR:
        return EXECUTION_ENGINE

    if executor_type in SUPPORTED_EXECUTORS:
        return executor_type

    raise TaskExecutionServiceError(
        f"Unsupported executor_type '{task.executor_type}'. "
        f"Supported executors: {sorted(SUPPORTED_EXECUTORS)}"
    )


def _create_execution_run_for_task(db: Session, task: Task) -> ExecutionRun:
    return create_execution_run(
        db=db,
        task_id=task.id,
        agent_name="execution_engine",
        input_snapshot=f"Executing task {task.id}: {task.title}",
    )


def _build_sync_result(
    *,
    task: Task,
    run_id: int,
    resolved_executor_type: str,
    run_status: str,
    output_snapshot: str | None,
    message: str,
    final_task_status: str | None = None,
    validation_decision: str | None = None,
) -> SyncTaskExecutionResult:
    return SyncTaskExecutionResult(
        task_id=task.id,
        execution_run_id=run_id,
        run_status=run_status,
        executor_type=resolved_executor_type,
        output_snapshot=output_snapshot,
        message=message,
        final_task_status=final_task_status,
        validation_decision=validation_decision,
    )


def _serialize_execution_agent_sequence(
    execution_agent_sequence: list[str] | None,
) -> str:
    return json.dumps(execution_agent_sequence or [], ensure_ascii=False)


def _store_task_execution_agent_sequence(
    db: Session,
    *,
    task: Task,
    execution_agent_sequence_json: str,
) -> None:
    task.last_execution_agent_sequence = execution_agent_sequence_json
    db.add(task)
    db.commit()
    db.refresh(task)


def _extract_artifacts_created_from_engine_result(result) -> str | None:
    artifact_refs = list(result.evidence.artifacts_created or [])
    if not artifact_refs:
        return None
    return " | ".join(artifact_refs)


def _prepare_execution_workspace(
    *,
    task: Task,
    run_id: int,
) -> None:
    """
    Prepare the isolated execution workspace for this execution run.

    This must happen before building the execution request and before the
    execution engine starts, so that:
    - the workspace path exists
    - source baseline is copied into the run workspace
    - validation can later inspect the same isolated workspace
    """
    try:
        storage_service = ProjectStorageService()
        workspace_runtime = LocalWorkspaceRuntime(storage_service=storage_service)

        prepared = workspace_runtime.prepare_workspace(
            project_id=task.project_id,
            execution_run_id=run_id,
            domain_name=CODE_DOMAIN,
        )

        logger.info(
            "execution_workspace_prepared task_id=%s run_id=%s project_id=%s workspace=%s source=%s",
            task.id,
            run_id,
            task.project_id,
            str(prepared.workspace_dir),
            str(prepared.source_dir) if prepared.source_dir else "",
        )
    except Exception as exc:
        raise TaskExecutionServiceError(
            f"Could not prepare execution workspace for task {task.id}, run {run_id}: {str(exc)}"
        ) from exc


def _promote_validated_workspace_to_source(
    *,
    task: Task,
    run_id: int,
) -> None:
    """
    Promote the isolated execution workspace into canonical project source.

    This is intentionally executed only after:
    - execution finished
    - validation produced decision='completed'

    And before:
    - the final task status is persisted as completed

    This helper must not persist task or run state. Callers are responsible
    for degrading the orchestration state coherently if promotion fails.
    """
    try:
        storage_service = ProjectStorageService()
        workspace_runtime = LocalWorkspaceRuntime(storage_service=storage_service)
        workspace_runtime.promote_workspace_to_source(
            project_id=task.project_id,
            execution_run_id=run_id,
            domain_name=CODE_DOMAIN,
        )
    except Exception as exc:
        raise TaskExecutionServiceError(
            f"Task {task.id} passed validation but its workspace could not be promoted to source: {str(exc)}"
        ) from exc


def _reconcile_hierarchy_or_raise(
    db: Session,
    *,
    affected_task_ids: list[int],
) -> None:
    try:
        reconcile_task_hierarchy_after_changes(
            db=db,
            affected_task_ids=affected_task_ids,
        )
    except TaskHierarchyReconciliationServiceError as exc:
        raise TaskExecutionServiceError(
            f"Task hierarchy reconciliation failed after terminal task update: {str(exc)}"
        ) from exc


def _collect_persisted_artifacts_for_task(
    db: Session,
    *,
    task_id: int,
) -> list[Artifact]:
    return (
        db.query(Artifact)
        .filter(Artifact.task_id == task_id)
        .order_by(Artifact.id.asc())
        .all()
    )


def _persist_task_status(
    db: Session,
    *,
    task_id: int,
    status: str,
) -> str:
    task = _get_task_or_raise(db, task_id)
    task.status = status
    db.add(task)
    db.commit()
    db.refresh(task)
    return task.status


def _apply_validation_result_to_task(
    db: Session,
    *,
    task_id: int,
    validation_decision: str,
    final_task_status: str | None,
) -> str:
    if final_task_status:
        return _persist_task_status(
            db,
            task_id=task_id,
            status=final_task_status,
        )

    if validation_decision == "completed":
        return _persist_task_status(
            db,
            task_id=task_id,
            status=TASK_STATUS_COMPLETED,
        )

    if validation_decision == "partial":
        return _persist_task_status(
            db,
            task_id=task_id,
            status=TASK_STATUS_PARTIAL,
        )

    if validation_decision in {"failed", "manual_review"}:
        mark_task_failed(db, task_id)
        return _get_task_or_raise(db, task_id).status

    raise TaskExecutionServiceError(
        f"Unsupported validation decision '{validation_decision}'."
    )


def _serialize_validation_result_artifact(
    *,
    task: Task,
    run_id: int,
    validation_service_result: ValidationServiceResult,
    final_task_status: str,
    workspace_promoted_to_source: bool,
) -> dict[str, Any]:
    validation_result = validation_service_result.validation_result
    routing_decision = validation_service_result.routing_decision

    return {
        "project_id": task.project_id,
        "task_id": task.id,
        "execution_run_id": run_id,
        "artifact_type": VALIDATION_RESULT_ARTIFACT_TYPE,
        "validator_key": validation_result.validator_key,
        "discipline": validation_result.discipline,
        "validation_mode": routing_decision.validation_mode,
        "decision": validation_result.decision,
        "summary": validation_result.summary,
        "validated_scope": validation_result.validated_scope,
        "missing_scope": validation_result.missing_scope,
        "blockers": list(validation_result.blockers or []),
        "manual_review_required": validation_result.manual_review_required,
        "followup_validation_required": validation_result.followup_validation_required,
        "recommended_next_validator_keys": list(
            validation_result.recommended_next_validator_keys or []
        ),
        "partial_validation_summary": validation_result.partial_validation_summary,
        "final_task_status": final_task_status,
        "workspace_promoted_to_source": workspace_promoted_to_source,
        "validated_evidence_ids": list(validation_result.validated_evidence_ids or []),
        "unconsumed_evidence_ids": list(validation_result.unconsumed_evidence_ids or []),
        "findings": [
            finding.model_dump(mode="json") for finding in validation_result.findings
        ],
        "metadata": dict(validation_result.metadata or {}),
    }


def _persist_validation_result_artifact(
    db: Session,
    *,
    task: Task,
    run_id: int,
    validation_service_result: ValidationServiceResult,
    final_task_status: str,
    workspace_promoted_to_source: bool,
) -> Artifact:
    artifact_payload = _serialize_validation_result_artifact(
        task=task,
        run_id=run_id,
        validation_service_result=validation_service_result,
        final_task_status=final_task_status,
        workspace_promoted_to_source=workspace_promoted_to_source,
    )

    return create_artifact(
        db=db,
        project_id=task.project_id,
        task_id=task.id,
        artifact_type=VALIDATION_RESULT_ARTIFACT_TYPE,
        content=json.dumps(artifact_payload, ensure_ascii=False),
        created_by=VALIDATION_RESULT_ARTIFACT_CREATED_BY,
        auto_commit=True,
    )


def _mark_task_and_run_failed_after_post_execution_error(
    db: Session,
    *,
    task: Task,
    run_id: int,
    execution_result,
    failure_code: str,
    error_message: str,
    validation_notes: list[str] | None = None,
) -> None:
    execution_agent_sequence_json = _serialize_execution_agent_sequence(
        execution_result.execution_agent_sequence
    )

    mark_execution_run_failed(
        db=db,
        run_id=run_id,
        error_message=error_message,
        failure_type=FAILURE_TYPE_INTERNAL,
        failure_code=failure_code,
        recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
        work_summary=execution_result.summary,
        work_details=execution_result.details,
        execution_agent_sequence=execution_agent_sequence_json,
        artifacts_created=_extract_artifacts_created_from_engine_result(execution_result),
        completed_scope=execution_result.completed_scope,
        remaining_scope=execution_result.remaining_scope,
        blockers_found=(
            "; ".join(execution_result.blockers_found)
            if execution_result.blockers_found
            else None
        ),
        validation_notes="; ".join(
            (execution_result.validation_notes or []) + (validation_notes or [])
        )
        or None,
    )
    _store_task_execution_agent_sequence(
        db=db,
        task=task,
        execution_agent_sequence_json=execution_agent_sequence_json,
    )
    mark_task_failed(db, task.id)


def _attempt_reconcile_after_failure(
    db: Session,
    *,
    affected_task_ids: list[int],
) -> None:
    try:
        _reconcile_hierarchy_or_raise(
            db=db,
            affected_task_ids=affected_task_ids,
        )
    except TaskExecutionServiceError:
        logger.exception(
            "task_hierarchy_reconciliation_failed_during_failure_handling affected_task_ids=%s",
            affected_task_ids,
        )


def _resolve_final_task_status(
    *,
    validation_decision: str,
    validation_result_final_task_status: str | None,
) -> str:
    if validation_result_final_task_status:
        return validation_result_final_task_status

    if validation_decision == "completed":
        return TASK_STATUS_COMPLETED

    if validation_decision == "partial":
        return TASK_STATUS_PARTIAL

    if validation_decision in {"failed", "manual_review"}:
        return TASK_STATUS_FAILED

    raise TaskExecutionServiceError(
        f"Unsupported validation decision '{validation_decision}'."
    )


def _assert_validation_post_conditions(
    db: Session,
    *,
    task_id: int,
    run_id: int,
) -> None:
    task = _get_task_or_raise(db, task_id)

    artifacts = (
        db.query(Artifact)
        .filter(
            Artifact.task_id == task_id,
            Artifact.artifact_type == VALIDATION_RESULT_ARTIFACT_TYPE,
        )
        .order_by(Artifact.id.asc())
        .all()
    )

    if not artifacts:
        raise TaskExecutionServiceError(
            "Invariant violation: validation flow completed without validation_result artifact."
        )

    matching_artifacts = []
    for artifact in artifacts:
        try:
            payload = json.loads(artifact.content or "{}")
        except Exception as exc:
            raise TaskExecutionServiceError(
                "Invariant violation: validation_result artifact content is not valid JSON."
            ) from exc

        if payload.get("execution_run_id") == run_id:
            matching_artifacts.append((artifact, payload))

    if not matching_artifacts:
        raise TaskExecutionServiceError(
            "Invariant violation: no validation_result artifact is linked to the current execution run."
        )

    if task.status not in {
        TASK_STATUS_COMPLETED,
        TASK_STATUS_PARTIAL,
        TASK_STATUS_FAILED,
    }:
        raise TaskExecutionServiceError(
            "Invariant violation: validation_result artifact exists but task is not in a terminal state."
        )

    if len(matching_artifacts) > 1:
        raise TaskExecutionServiceError(
            "Invariant violation: multiple validation_result artifacts are linked to the same execution run."
        )

    _, payload = matching_artifacts[0]

    if payload.get("task_id") != task_id:
        raise TaskExecutionServiceError(
            "Invariant violation: validation_result artifact task_id does not match the current task."
        )

    if payload.get("final_task_status") != task.status:
        raise TaskExecutionServiceError(
            "Invariant violation: validation_result artifact final_task_status does not match the persisted task status."
        )
    

def _validate_after_execution(
    db: Session,
    *,
    task: Task,
    run_id: int,
    resolved_executor_type: str,
    execution_request,
    execution_result,
) -> SyncTaskExecutionResult:
    execution_run = _get_execution_run_or_raise(db, run_id)
    persisted_artifacts = _collect_persisted_artifacts_for_task(
        db,
        task_id=task.id,
    )

    try:
        validation_service_result = validate_execution_result(
            task=task,
            execution_request=execution_request,
            execution_result=execution_result,
            execution_run=execution_run,
            persisted_artifacts=persisted_artifacts,
        )
    except ValidationServiceError as exc:
        error_message = (
            f"Execution finished but validation could not be completed for task {task.id}: {str(exc)}"
        )
        _mark_task_and_run_failed_after_post_execution_error(
            db=db,
            task=task,
            run_id=run_id,
            execution_result=execution_result,
            failure_code="validation_service_error",
            error_message=error_message,
            validation_notes=["Validation service failure after execution."],
        )
        raise TaskExecutionServiceError(error_message) from exc

    validation_result = validation_service_result.validation_result
    decision = validation_result.decision

    logger.info(
        "task_validation_completed task_id=%s run_id=%s validator=%s discipline=%s decision=%s followup_required=%s",
        task.id,
        run_id,
        validation_result.validator_key,
        validation_result.discipline,
        decision,
        validation_result.followup_validation_required,
    )

    if decision == "completed":
        message = (
            "Execution and validation completed successfully, and the validated workspace "
            "was promoted to source before closing the task."
        )
    elif decision == "partial":
        if validation_result.followup_validation_required:
            message = (
                "Execution finished and validation is partial. Additional validation "
                "follow-up is required for evidence not consumed by this validator."
            )
        else:
            message = "Execution finished and validation concluded the task is partial."
    elif decision == "failed":
        message = "Execution finished but validation concluded the task failed."
    elif decision == "manual_review":
        message = (
            "Execution finished but validation requires manual review before the task "
            "can be considered complete."
        )
    else:
        raise TaskExecutionServiceError(
            f"Unsupported validation decision '{decision}'."
        )

    workspace_promoted_to_source = False

    try:
        if decision == "completed":
            refreshed_task_for_promotion = _get_task_or_raise(db, task.id)
            _promote_validated_workspace_to_source(
                task=refreshed_task_for_promotion,
                run_id=run_id,
            )
            workspace_promoted_to_source = True

        final_task_status = _resolve_final_task_status(
            validation_decision=decision,
            validation_result_final_task_status=validation_result.final_task_status,
        )

        _persist_validation_result_artifact(
            db=db,
            task=task,
            run_id=run_id,
            validation_service_result=validation_service_result,
            final_task_status=final_task_status,
            workspace_promoted_to_source=workspace_promoted_to_source,
        )

        _persist_task_status(
            db=db,
            task_id=task.id,
            status=final_task_status,
        )

    except TaskExecutionServiceError as exc:
        _mark_task_and_run_failed_after_post_execution_error(
            db=db,
            task=task,
            run_id=run_id,
            execution_result=execution_result,
            failure_code="post_validation_processing_failed",
            error_message=str(exc),
            validation_notes=["Post-validation processing failed before task closure."],
        )
        raise
    except Exception as exc:
        error_message = (
            f"Execution finished but post-validation processing failed for task {task.id}: {str(exc)}"
        )
        _mark_task_and_run_failed_after_post_execution_error(
            db=db,
            task=task,
            run_id=run_id,
            execution_result=execution_result,
            failure_code="post_validation_processing_failed",
            error_message=error_message,
            validation_notes=["Post-validation processing failed before task closure."],
        )
        raise TaskExecutionServiceError(error_message) from exc

    _assert_validation_post_conditions(
        db=db,
        task_id=task.id,
        run_id=run_id,
    )

    try:
        _reconcile_hierarchy_or_raise(
            db=db,
            affected_task_ids=[task.id],
        )
    except TaskExecutionServiceError:
        raise

    refreshed_task = _get_task_or_raise(db, task.id)
    refreshed_run = _get_execution_run_or_raise(db, run_id)

    return _build_sync_result(
        task=refreshed_task,
        run_id=run_id,
        resolved_executor_type=resolved_executor_type,
        run_status=refreshed_run.status,
        output_snapshot=execution_result.output_snapshot,
        message=message,
        final_task_status=refreshed_task.status,
        validation_decision=decision,
    )


def _handle_terminal_execution_outcome(
    db: Session,
    *,
    task: Task,
    run_id: int,
    resolved_executor_type: str,
    engine_result,
) -> SyncTaskExecutionResult:
    execution_agent_sequence_json = _serialize_execution_agent_sequence(
        engine_result.execution_agent_sequence
    )
    _store_task_execution_agent_sequence(
        db=db,
        task=task,
        execution_agent_sequence_json=execution_agent_sequence_json,
    )

    blockers_found = (
        "; ".join(engine_result.blockers_found)
        if engine_result.blockers_found
        else None
    )

    if engine_result.decision == EXECUTION_DECISION_FAILED:
        mark_execution_run_failed(
            db=db,
            run_id=run_id,
            error_message=engine_result.summary or "Execution engine reported a failed execution.",
            failure_type=FAILURE_TYPE_INTERNAL,
            failure_code="execution_engine_failed",
            recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
            work_summary=engine_result.summary,
            work_details=engine_result.details,
            execution_agent_sequence=execution_agent_sequence_json,
            artifacts_created=_extract_artifacts_created_from_engine_result(engine_result),
            completed_scope=engine_result.completed_scope,
            remaining_scope=engine_result.remaining_scope,
            blockers_found=blockers_found,
            validation_notes="; ".join(engine_result.validation_notes or []),
        )
        mark_task_failed(db, task.id)
        _reconcile_hierarchy_or_raise(
            db=db,
            affected_task_ids=[task.id],
        )
        refreshed_task = _get_task_or_raise(db, task.id)
        refreshed_run = _get_execution_run_or_raise(db, run_id)

        return _build_sync_result(
            task=refreshed_task,
            run_id=run_id,
            resolved_executor_type=resolved_executor_type,
            run_status=refreshed_run.status,
            output_snapshot=engine_result.output_snapshot,
            message="Execution failed and the task was routed for recovery without validation.",
            final_task_status=refreshed_task.status,
            validation_decision=None,
        )

    if engine_result.decision == EXECUTION_DECISION_REJECTED:
        mark_execution_run_rejected(
            db=db,
            run_id=run_id,
            error_message=engine_result.summary or "Execution engine rejected the task.",
            failure_code="execution_engine_rejected",
            recovery_action=RECOVERY_ACTION_REATOMIZE,
            work_summary=engine_result.summary,
            work_details=engine_result.details,
            execution_agent_sequence=execution_agent_sequence_json,
            blockers_found=blockers_found,
            validation_notes="; ".join(engine_result.validation_notes or []),
        )
        mark_task_failed(db, task.id)
        _reconcile_hierarchy_or_raise(
            db=db,
            affected_task_ids=[task.id],
        )
        refreshed_task = _get_task_or_raise(db, task.id)
        refreshed_run = _get_execution_run_or_raise(db, run_id)

        return _build_sync_result(
            task=refreshed_task,
            run_id=run_id,
            resolved_executor_type=resolved_executor_type,
            run_status=refreshed_run.status,
            output_snapshot=engine_result.output_snapshot,
            message="Execution was rejected and the task was routed for recovery without validation.",
            final_task_status=refreshed_task.status,
            validation_decision=None,
        )

    raise TaskExecutionServiceError(
        f"Unsupported terminal execution decision '{engine_result.decision}'."
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
        resolved_executor_type = _resolve_executor_type_for_task(task)

        mark_execution_run_started(db, run_id)
        mark_task_running(db, task.id)

        logger.info(
            "execution_engine_starting task_id=%s run_id=%s project_id=%s executor_type=%s",
            task.id,
            run_id,
            task.project_id,
            resolved_executor_type,
        )

        _prepare_execution_workspace(
            task=task,
            run_id=run_id,
        )

        execution_request = build_execution_request(
            db=db,
            task=task,
            execution_run_id=run_id,
            resolved_executor_type=resolved_executor_type,
        )

        logger.info(
            "execution_request_built task_id=%s run_id=%s workspace=%s source=%s",
            task.id,
            run_id,
            execution_request.context.workspace_path,
            execution_request.context.source_path,
        )

        execution_engine = get_execution_engine(db)
        logger.info(
            "execution_engine_selected task_id=%s run_id=%s backend=%s",
            task.id,
            run_id,
            getattr(execution_engine, "backend_name", execution_engine.__class__.__name__),
        )

        engine_result = execution_engine.execute(execution_request)

        logger.info(
            "execution_engine_completed task_id=%s run_id=%s decision=%s summary=%s",
            task.id,
            run_id,
            engine_result.decision,
            engine_result.summary,
        )

        execution_agent_sequence_json = _serialize_execution_agent_sequence(
            engine_result.execution_agent_sequence
        )

        if engine_result.decision in {
            EXECUTION_DECISION_COMPLETED,
            EXECUTION_DECISION_PARTIAL,
        }:
            mark_execution_run_succeeded(
                db=db,
                run_id=run_id,
                output_snapshot=engine_result.output_snapshot,
                work_summary=engine_result.summary,
                work_details=engine_result.details,
                execution_agent_sequence=execution_agent_sequence_json,
                artifacts_created=_extract_artifacts_created_from_engine_result(
                    engine_result
                ),
                completed_scope=engine_result.completed_scope,
                validation_notes="; ".join(engine_result.validation_notes or []),
            )
            _store_task_execution_agent_sequence(
                db=db,
                task=task,
                execution_agent_sequence_json=execution_agent_sequence_json,
            )

            return _validate_after_execution(
                db=db,
                task=task,
                run_id=run_id,
                resolved_executor_type=resolved_executor_type,
                execution_request=execution_request,
                execution_result=engine_result,
            )

        if engine_result.decision in {
            EXECUTION_DECISION_FAILED,
            EXECUTION_DECISION_REJECTED,
        }:
            return _handle_terminal_execution_outcome(
                db=db,
                task=task,
                run_id=run_id,
                resolved_executor_type=resolved_executor_type,
                engine_result=engine_result,
            )

        raise TaskExecutionServiceError(
            f"Unsupported execution engine decision '{engine_result.decision}'."
        )

    except TaskExecutionServiceError:
        raise

    except ExecutionEngineRejectedError as exc:
        execution_agent_sequence_json = _serialize_execution_agent_sequence([])

        mark_execution_run_rejected(
            db=db,
            run_id=run_id,
            error_message=exc.message,
            failure_code=exc.failure_code,
            recovery_action=RECOVERY_ACTION_REATOMIZE,
            work_summary=exc.message,
            work_details="The execution engine deliberately rejected the task before execution.",
            execution_agent_sequence=execution_agent_sequence_json,
            blockers_found="; ".join(exc.blockers_found) if exc.blockers_found else None,
            validation_notes="; ".join(
                exc.validation_notes or ["Execution was rejected at the execution engine boundary."]
            ),
        )
        _store_task_execution_agent_sequence(
            db=db,
            task=task,
            execution_agent_sequence_json=execution_agent_sequence_json,
        )
        mark_task_failed(db, task.id)
        _reconcile_hierarchy_or_raise(
            db=db,
            affected_task_ids=[task.id],
        )

        refreshed_task = _get_task_or_raise(db, task.id)
        refreshed_run = _get_execution_run_or_raise(db, run_id)

        return _build_sync_result(
            task=refreshed_task,
            run_id=run_id,
            resolved_executor_type=_resolve_executor_type_for_task(task),
            run_status=refreshed_run.status,
            output_snapshot=None,
            message="Execution was rejected at the execution engine boundary and routed for recovery without validation.",
            final_task_status=refreshed_task.status,
            validation_decision=None,
        )

    except ExecutionEngineError as exc:
        execution_agent_sequence_json = _serialize_execution_agent_sequence([])

        mark_execution_run_failed(
            db=db,
            run_id=run_id,
            error_message=str(exc),
            failure_type=FAILURE_TYPE_INTERNAL,
            failure_code="execution_engine_error",
            recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
            execution_agent_sequence=execution_agent_sequence_json,
            validation_notes="Execution engine internal failure.",
        )
        _store_task_execution_agent_sequence(
            db=db,
            task=task,
            execution_agent_sequence_json=execution_agent_sequence_json,
        )
        mark_task_failed(db, task.id)
        _reconcile_hierarchy_or_raise(
            db=db,
            affected_task_ids=[task.id],
        )

        refreshed_task = _get_task_or_raise(db, task.id)
        refreshed_run = _get_execution_run_or_raise(db, run_id)

        return _build_sync_result(
            task=refreshed_task,
            run_id=run_id,
            resolved_executor_type=_resolve_executor_type_for_task(task),
            run_status=refreshed_run.status,
            output_snapshot=None,
            message="Execution failed due to an internal execution engine error and was routed for recovery without validation.",
            final_task_status=refreshed_task.status,
            validation_decision=None,
        )

    except Exception as exc:
        execution_agent_sequence_json = _serialize_execution_agent_sequence([])

        mark_execution_run_failed(
            db=db,
            run_id=run_id,
            error_message=str(exc),
            failure_type=FAILURE_TYPE_INTERNAL,
            failure_code="task_execution_service_error",
            recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
            execution_agent_sequence=execution_agent_sequence_json,
        )
        _store_task_execution_agent_sequence(
            db=db,
            task=task,
            execution_agent_sequence_json=execution_agent_sequence_json,
        )
        mark_task_failed(db, task.id)
        _reconcile_hierarchy_or_raise(
            db=db,
            affected_task_ids=[task.id],
        )

        refreshed_task = _get_task_or_raise(db, task.id)
        refreshed_run = _get_execution_run_or_raise(db, run_id)

        return _build_sync_result(
            task=refreshed_task,
            run_id=run_id,
            resolved_executor_type=_resolve_executor_type_for_task(task),
            run_status=refreshed_run.status,
            output_snapshot=None,
            message="Execution failed due to an unexpected orchestration error and was routed for recovery without validation.",
            final_task_status=refreshed_task.status,
            validation_decision=None,
        )


def execute_task_sync(db: Session, task_id: int) -> SyncTaskExecutionResult:
    task = _get_task_or_raise(db, task_id)
    _validate_task_is_executable(task)
    run = _create_execution_run_for_task(db, task)
    return execute_existing_run_sync(db, run.id)