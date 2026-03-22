from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.schemas.code_execution import (
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_EXECUTION_STATUS_FAILED,
    CODE_EXECUTION_STATUS_REJECTED,
)
from app.models.task import Task
from app.services.code_executor import (
    CodeExecutorInternalError,
    CodeExecutorRejectedError,
    LocalCodeExecutor,
)


class ExecutorServiceError(Exception):
    """Base exception for compatibility executor service."""


class ExecutorRejectedError(ExecutorServiceError):
    """Compatibility wrapper for rejected execution."""

    def __init__(
        self,
        message: str,
        failure_code: str,
        work_summary: str,
        work_details: str,
        blockers_found: str,
        validation_notes: str,
    ):
        super().__init__(message)
        self.message = message
        self.failure_code = failure_code
        self.work_summary = work_summary
        self.work_details = work_details
        self.blockers_found = blockers_found
        self.validation_notes = validation_notes


class ExecutorInternalError(ExecutorServiceError):
    """Compatibility wrapper for internal execution failure."""

    def __init__(self, message: str, failure_code: str = "internal_executor_error"):
        super().__init__(message)
        self.message = message
        self.failure_code = failure_code


@dataclass
class ExecutorResult:
    status: str
    artifact_type: str | None
    output_snapshot: str
    artifact_id: int | None
    work_summary: str
    work_details: str
    artifacts_created: str | None
    completed_scope: str | None
    remaining_scope: str | None
    blockers_found: str | None
    validation_notes: str


def execute_atomic_task(
    db: Session,
    task: Task,
    execution_run_id: int,
) -> ExecutorResult:
    """
    Compatibility facade over the code executor.

    Valid outcomes:
      - awaiting_validation
      - rejected
      - failed
    """
    try:
        executor = LocalCodeExecutor(db=db)
        result = executor.execute(task=task, execution_run_id=execution_run_id)

        persisted_artifact_ids = [
            note.split("artifact_id=")[-1].strip(".")
            for note in result.journal.notes_for_validator
            if "artifact_id=" in note
        ]
        artifact_id = int(persisted_artifact_ids[-1]) if persisted_artifact_ids else None

        return ExecutorResult(
            status=CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
            artifact_type="code_executor_result",
            output_snapshot=result.output_snapshot or "code_executor_result_created",
            artifact_id=artifact_id,
            work_summary=result.journal.summary,
            work_details=result.edit_plan.summary,
            artifacts_created=(
                f"code_executor_result:{artifact_id}" if artifact_id is not None else None
            ),
            completed_scope=result.journal.claimed_completed_scope,
            remaining_scope=result.journal.claimed_remaining_scope,
            blockers_found=(
                "; ".join(result.journal.encountered_uncertainties)
                if result.journal.encountered_uncertainties
                else None
            ),
            validation_notes="; ".join(result.journal.notes_for_validator),
        )

    except CodeExecutorRejectedError as exc:
        return ExecutorResult(
            status=CODE_EXECUTION_STATUS_REJECTED,
            artifact_type=None,
            output_snapshot="code_executor_rejected",
            artifact_id=None,
            work_summary=exc.message,
            work_details="The executor deliberately rejected the task before execution.",
            artifacts_created=None,
            completed_scope=None,
            remaining_scope=exc.remaining_scope,
            blockers_found=exc.blockers_found,
            validation_notes=(
                "Execution was rejected at the executor boundary. "
                "The task needs redefinition, richer context, or reassignment."
            ),
        )

    except CodeExecutorInternalError as exc:
        return ExecutorResult(
            status=CODE_EXECUTION_STATUS_FAILED,
            artifact_type=None,
            output_snapshot="code_executor_failed",
            artifact_id=None,
            work_summary=exc.message,
            work_details="The executor attempted execution and failed internally.",
            artifacts_created=None,
            completed_scope=None,
            remaining_scope=None,
            blockers_found=None,
            validation_notes=f"Internal execution failure. failure_code={exc.failure_code}",
        )

    except Exception as exc:
        return ExecutorResult(
            status=CODE_EXECUTION_STATUS_FAILED,
            artifact_type=None,
            output_snapshot="code_executor_failed",
            artifact_id=None,
            work_summary=f"Executor failed unexpectedly: {str(exc)}",
            work_details="Unexpected executor failure outside the expected code executor flow.",
            artifacts_created=None,
            completed_scope=None,
            remaining_scope=None,
            blockers_found=None,
            validation_notes="Unexpected executor failure.",
        )