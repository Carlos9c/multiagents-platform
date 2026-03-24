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
from app.schemas.code_execution import (
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_EXECUTION_STATUS_FAILED,
    CODE_EXECUTION_STATUS_REJECTED,
    CodeExecutorInput,
    CodeExecutorResult,
    CodeFileEditPlan,
    CodeWorkingSet,
    ExecutionJournal,
    WorkspaceChangeSet,
)
from app.schemas.code_validation import (
    CODE_VALIDATION_DECISION_COMPLETED,
    CODE_VALIDATION_DECISION_FAILED,
    CODE_VALIDATION_DECISION_PARTIAL,
)
from app.services.code_executor import (
    CodeExecutorInternalError,
    CodeExecutorRejectedError,
    LocalCodeExecutor,
)
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
from app.services.task_validation_service import (
    TaskValidationServiceError,
    apply_validation_decision_to_task,
    validate_code_task,
    validate_terminal_code_task,
)
from app.services.tasks import (
    mark_task_failed,
    mark_task_running,
)


SUPPORTED_EXECUTORS = {
    CODE_EXECUTOR,
}

VALID_EXECUTOR_FINAL_STATUSES = {
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_EXECUTION_STATUS_FAILED,
    CODE_EXECUTION_STATUS_REJECTED,
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
    final_task_status: str | None = None
    validation_decision: str | None = None


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


def _build_sync_result(
    *,
    task: Task,
    run_id: int,
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
        executor_type=task.executor_type,
        output_snapshot=output_snapshot,
        message=message,
        final_task_status=final_task_status,
        validation_decision=validation_decision,
    )


def _extract_artifacts_created(executor_result: CodeExecutorResult) -> str | None:
    artifact_refs: list[str] = []

    for note in executor_result.journal.notes_for_validator:
        if "artifact_id=" in note:
            artifact_refs.append(note.strip())

    if not artifact_refs:
        return None

    return " | ".join(artifact_refs)


def _split_blockers(blockers_found: str | None) -> list[str]:
    if not blockers_found:
        return []
    return [item.strip() for item in blockers_found.split(";") if item.strip()]


def _build_synthetic_executor_result(
    *,
    task: Task,
    execution_status: str,
    summary: str,
    work_details: str | None = None,
    remaining_scope: str | None = None,
    blockers_found: str | None = None,
    validation_notes: list[str] | None = None,
    output_snapshot: str | None = None,
) -> CodeExecutorResult:
    return CodeExecutorResult(
        task_id=task.id,
        execution_status=execution_status,
        input=CodeExecutorInput(
            task_id=task.id,
            project_id=task.project_id,
            title=task.title,
            description=task.description,
            objective=task.objective,
            acceptance_criteria=task.acceptance_criteria,
            technical_constraints=task.technical_constraints,
            out_of_scope=task.out_of_scope,
            execution_goal=(
                task.objective
                or task.summary
                or task.description
                or f"Execute task {task.id}: {task.title}"
            ),
            repo_root="",
            relevant_decisions=[],
            candidate_modules=[],
            candidate_files=[],
            primary_targets=[],
            related_files=[],
            reference_files=[],
            related_test_files=[],
            relevant_symbols=[],
            unresolved_questions=[],
            selection_rationale=(
                "Synthetic execution context created by task_execution_service "
                "to persist terminal validation evidence after pre-validation failure."
            ),
            selection_confidence=0.0,
        ),
        working_set=CodeWorkingSet(
            repo_root="",
            target_files=[],
            related_files=[],
            reference_files=[],
            test_files=[],
            files=[],
            repo_guidance=[],
        ),
        edit_plan=CodeFileEditPlan(
            task_id=task.id,
            summary=work_details or "No executable edit plan was completed before termination.",
            planned_changes=[],
            assumptions=[],
            local_risks=[],
            notes=[
                "Synthetic edit plan generated after terminal executor failure/rejection.",
            ],
        ),
        workspace_changes=WorkspaceChangeSet(
            created_files=[],
            modified_files=[],
            deleted_files=[],
            renamed_files=[],
            diff_summary=None,
            impacted_areas=[],
        ),
        journal=ExecutionJournal(
            task_id=task.id,
            summary=summary,
            local_decisions=[],
            claimed_completed_scope=None,
            claimed_remaining_scope=remaining_scope,
            encountered_uncertainties=_split_blockers(blockers_found),
            notes_for_validator=validation_notes or [],
        ),
        output_snapshot=output_snapshot,
    )


def _promote_validated_workspace_to_source(
    db: Session,
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
        mark_task_failed(db, task.id)
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


def _validate_after_execution(
    db: Session,
    *,
    task: Task,
    run_id: int,
    executor_result: CodeExecutorResult,
) -> SyncTaskExecutionResult:
    try:
        validation_service_result = validate_code_task(
            db=db,
            task_id=task.id,
            execution_run_id=run_id,
            executor_result=executor_result,
            apply_final_status=False,
        )
    except TaskValidationServiceError as exc:
        mark_task_failed(db, task.id)
        raise TaskExecutionServiceError(
            f"Execution finished but validation could not be completed for task {task.id}: {str(exc)}"
        ) from exc

    decision = validation_service_result.validation_result.decision

    if decision == CODE_VALIDATION_DECISION_COMPLETED:
        refreshed_task_for_promotion = _get_task_or_raise(db, task.id)
        _promote_validated_workspace_to_source(
            db=db,
            task=refreshed_task_for_promotion,
            run_id=run_id,
        )
        final_task_status = apply_validation_decision_to_task(
            db=db,
            task_id=task.id,
            decision=decision,
        )
        message = (
            "Execution and validation completed successfully, and the validated workspace "
            "was promoted to source before closing the task."
        )
    elif decision == CODE_VALIDATION_DECISION_PARTIAL:
        final_task_status = apply_validation_decision_to_task(
            db=db,
            task_id=task.id,
            decision=decision,
        )
        message = "Execution finished and validation concluded the task is partial."
    elif decision == CODE_VALIDATION_DECISION_FAILED:
        final_task_status = apply_validation_decision_to_task(
            db=db,
            task_id=task.id,
            decision=decision,
        )
        message = "Execution finished but validation concluded the task failed."
    else:
        raise TaskExecutionServiceError(
            f"Unsupported validation decision '{decision}'."
        )

    _reconcile_hierarchy_or_raise(
        db=db,
        affected_task_ids=[task.id],
    )

    refreshed_task = _get_task_or_raise(db, task.id)

    return _build_sync_result(
        task=refreshed_task,
        run_id=run_id,
        run_status=CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
        output_snapshot=executor_result.output_snapshot,
        message=message,
        final_task_status=final_task_status,
        validation_decision=decision,
    )


def _finalize_prevalidation_terminal_outcome(
    db: Session,
    *,
    task: Task,
    run_id: int,
    run_status: str,
    executor_result: CodeExecutorResult,
    message: str,
) -> SyncTaskExecutionResult:
    try:
        validation_service_result = validate_terminal_code_task(
            db=db,
            task_id=task.id,
            execution_run_id=run_id,
            executor_result=executor_result,
            apply_final_status=True,
        )
    except TaskValidationServiceError as exc:
        mark_task_failed(db, task.id)
        raise TaskExecutionServiceError(
            "Execution reached a terminal state before validation, and the service "
            f"could not persist the required validation artifact for task {task.id}: {str(exc)}"
        ) from exc

    _reconcile_hierarchy_or_raise(
        db=db,
        affected_task_ids=[task.id],
    )

    refreshed_task = _get_task_or_raise(db, task.id)

    return _build_sync_result(
        task=refreshed_task,
        run_id=run_id,
        run_status=run_status,
        output_snapshot=executor_result.output_snapshot,
        message=message,
        final_task_status=validation_service_result.final_task_status,
        validation_decision=validation_service_result.validation_result.decision,
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

        executor = LocalCodeExecutor(db=db)
        executor_result = executor.execute(
            task=task,
            execution_run_id=run_id,
        )

        if executor_result.execution_status not in VALID_EXECUTOR_FINAL_STATUSES:
            raise TaskExecutionServiceError(
                f"Unsupported executor result status '{executor_result.execution_status}' returned by executor. "
                f"Allowed statuses: {sorted(VALID_EXECUTOR_FINAL_STATUSES)}"
            )

        if executor_result.execution_status == CODE_EXECUTION_STATUS_AWAITING_VALIDATION:
            mark_execution_run_succeeded(
                db=db,
                run_id=run_id,
                output_snapshot=executor_result.output_snapshot,
                work_summary=executor_result.journal.summary,
                work_details=executor_result.edit_plan.summary,
                artifacts_created=_extract_artifacts_created(executor_result),
                completed_scope=executor_result.journal.claimed_completed_scope,
                validation_notes="; ".join(executor_result.journal.notes_for_validator),
            )

            return _validate_after_execution(
                db=db,
                task=task,
                run_id=run_id,
                executor_result=executor_result,
            )

        if executor_result.execution_status == CODE_EXECUTION_STATUS_FAILED:
            blockers_found = (
                "; ".join(executor_result.journal.encountered_uncertainties)
                if executor_result.journal.encountered_uncertainties
                else None
            )

            mark_execution_run_failed(
                db=db,
                run_id=run_id,
                error_message=executor_result.journal.summary or "Executor reported a failed execution.",
                failure_type=FAILURE_TYPE_INTERNAL,
                failure_code="executor_failed",
                recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
                work_summary=executor_result.journal.summary,
                work_details=executor_result.edit_plan.summary,
                artifacts_created=_extract_artifacts_created(executor_result),
                completed_scope=executor_result.journal.claimed_completed_scope,
                remaining_scope=executor_result.journal.claimed_remaining_scope,
                blockers_found=blockers_found,
                validation_notes="; ".join(executor_result.journal.notes_for_validator),
            )
            mark_task_failed(db, task.id)

            return _finalize_prevalidation_terminal_outcome(
                db=db,
                task=task,
                run_id=run_id,
                run_status=CODE_EXECUTION_STATUS_FAILED,
                executor_result=executor_result,
                message="Execution failed before normal validation, but a terminal validation artifact was persisted.",
            )

        if executor_result.execution_status == CODE_EXECUTION_STATUS_REJECTED:
            blockers_found = (
                "; ".join(executor_result.journal.encountered_uncertainties)
                if executor_result.journal.encountered_uncertainties
                else None
            )

            mark_execution_run_rejected(
                db=db,
                run_id=run_id,
                error_message=executor_result.journal.summary or "Executor rejected the task.",
                failure_code="executor_rejected",
                recovery_action=RECOVERY_ACTION_REATOMIZE,
                work_summary=executor_result.journal.summary,
                work_details=executor_result.edit_plan.summary,
                blockers_found=blockers_found,
                validation_notes="; ".join(executor_result.journal.notes_for_validator),
            )
            mark_task_failed(db, task.id)

            return _finalize_prevalidation_terminal_outcome(
                db=db,
                task=task,
                run_id=run_id,
                run_status=CODE_EXECUTION_STATUS_REJECTED,
                executor_result=executor_result,
                message="Execution was rejected before normal validation, but a terminal validation artifact was persisted.",
            )

        raise TaskExecutionServiceError(
            f"Unhandled executor status '{executor_result.execution_status}'."
        )

    except TaskExecutionServiceError:
        raise

    except CodeExecutorRejectedError as exc:
        mark_execution_run_rejected(
            db=db,
            run_id=run_id,
            error_message=exc.message,
            failure_code=exc.failure_code,
            recovery_action=RECOVERY_ACTION_REATOMIZE,
            work_summary=exc.message,
            work_details="The executor deliberately rejected the task before execution.",
            blockers_found=exc.blockers_found,
            validation_notes="Execution was rejected at the executor boundary.",
        )
        mark_task_failed(db, task.id)

        synthetic_result = _build_synthetic_executor_result(
            task=task,
            execution_status=CODE_EXECUTION_STATUS_REJECTED,
            summary=exc.message,
            work_details="The executor deliberately rejected the task before execution.",
            remaining_scope=exc.remaining_scope,
            blockers_found=exc.blockers_found,
            validation_notes=[
                "Execution was rejected at the executor boundary.",
                f"failure_code={exc.failure_code}",
            ],
            output_snapshot=None,
        )

        return _finalize_prevalidation_terminal_outcome(
            db=db,
            task=task,
            run_id=run_id,
            run_status=CODE_EXECUTION_STATUS_REJECTED,
            executor_result=synthetic_result,
            message="Execution was rejected before validation, and a synthetic terminal validation artifact was persisted.",
        )

    except CodeExecutorInternalError as exc:
        mark_execution_run_failed(
            db=db,
            run_id=run_id,
            error_message=exc.message,
            failure_type=FAILURE_TYPE_INTERNAL,
            failure_code=exc.failure_code,
            recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
            validation_notes="Internal execution failure.",
        )
        mark_task_failed(db, task.id)

        synthetic_result = _build_synthetic_executor_result(
            task=task,
            execution_status=CODE_EXECUTION_STATUS_FAILED,
            summary=exc.message,
            work_details="The code executor raised an internal error before producing a normal result.",
            remaining_scope="Task execution stopped before a valid operational pass completed.",
            blockers_found="Internal executor failure.",
            validation_notes=[
                "Internal execution failure.",
                f"failure_code={exc.failure_code}",
            ],
            output_snapshot=None,
        )

        return _finalize_prevalidation_terminal_outcome(
            db=db,
            task=task,
            run_id=run_id,
            run_status=CODE_EXECUTION_STATUS_FAILED,
            executor_result=synthetic_result,
            message="Execution failed before validation, and a synthetic terminal validation artifact was persisted.",
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
        mark_task_failed(db, task.id)

        synthetic_result = _build_synthetic_executor_result(
            task=task,
            execution_status=CODE_EXECUTION_STATUS_FAILED,
            summary=str(exc),
            work_details="task_execution_service caught an unexpected exception before normal validation.",
            remaining_scope="Task execution stopped before a valid operational pass completed.",
            blockers_found="Unexpected execution orchestration error.",
            validation_notes=[
                "Unexpected task execution service error.",
                "failure_code=task_execution_service_error",
            ],
            output_snapshot=None,
        )

        return _finalize_prevalidation_terminal_outcome(
            db=db,
            task=task,
            run_id=run_id,
            run_status=CODE_EXECUTION_STATUS_FAILED,
            executor_result=synthetic_result,
            message="Execution failed before validation due to an unexpected service error, and a synthetic terminal validation artifact was persisted.",
        )


def execute_task_sync(db: Session, task_id: int) -> SyncTaskExecutionResult:
    task = _get_task_or_raise(db, task_id)
    _validate_task_is_executable(task)
    run = _create_execution_run_for_task(db, task)
    return execute_existing_run_sync(db, run.id)