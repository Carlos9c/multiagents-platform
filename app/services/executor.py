import re
from dataclasses import dataclass

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
from app.models.task import Task
from app.schemas.code_execution import (
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_EXECUTION_STATUS_FAILED,
    CODE_EXECUTION_STATUS_REJECTED,
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


def _extract_artifact_id(artifact_refs: list[str]) -> int | None:
    for item in reversed(artifact_refs):
        match = re.search(r"artifact_id=(\d+)", item)
        if match:
            return int(match.group(1))
    return None


def execute_atomic_task(
    db: Session,
    task: Task,
    execution_run_id: int,
) -> ExecutorResult:
    """
    Compatibility facade over the execution engine.

    Valid outcomes:
      - awaiting_validation
      - rejected
      - failed
    """
    try:
        request = build_execution_request(
            db=db,
            task=task,
            execution_run_id=execution_run_id,
        )
        engine = get_execution_engine(db)
        result = engine.execute(request)

        artifact_id = _extract_artifact_id(result.evidence.artifacts_created)

        if result.decision in {
            EXECUTION_DECISION_PARTIAL,
            EXECUTION_DECISION_COMPLETED,
        }:
            return ExecutorResult(
                status=CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
                artifact_type="execution_engine_result",
                output_snapshot=result.output_snapshot or "execution_engine_result_created",
                artifact_id=artifact_id,
                work_summary=result.summary,
                work_details=result.details or result.summary,
                artifacts_created=(
                    f"execution_engine_result:{artifact_id}" if artifact_id is not None else None
                ),
                completed_scope=result.completed_scope,
                remaining_scope=result.remaining_scope,
                blockers_found=(
                    "; ".join(result.blockers_found) if result.blockers_found else None
                ),
                validation_notes="; ".join(result.validation_notes),
            )

        if result.decision == EXECUTION_DECISION_REJECTED:
            return ExecutorResult(
                status=CODE_EXECUTION_STATUS_REJECTED,
                artifact_type=None,
                output_snapshot=result.output_snapshot or "execution_engine_rejected",
                artifact_id=None,
                work_summary=result.summary,
                work_details=result.details or "The execution engine deliberately rejected the task.",
                artifacts_created=None,
                completed_scope=result.completed_scope,
                remaining_scope=result.remaining_scope,
                blockers_found=(
                    "; ".join(result.blockers_found) if result.blockers_found else None
                ),
                validation_notes="; ".join(result.validation_notes),
            )

        if result.decision == EXECUTION_DECISION_FAILED:
            return ExecutorResult(
                status=CODE_EXECUTION_STATUS_FAILED,
                artifact_type=None,
                output_snapshot=result.output_snapshot or "execution_engine_failed",
                artifact_id=None,
                work_summary=result.summary,
                work_details=result.details or "The execution engine attempted execution and failed.",
                artifacts_created=None,
                completed_scope=result.completed_scope,
                remaining_scope=result.remaining_scope,
                blockers_found=(
                    "; ".join(result.blockers_found) if result.blockers_found else None
                ),
                validation_notes="; ".join(result.validation_notes),
            )

        return ExecutorResult(
            status=CODE_EXECUTION_STATUS_FAILED,
            artifact_type=None,
            output_snapshot="execution_engine_failed",
            artifact_id=None,
            work_summary=f"Unsupported execution engine decision: {result.decision}",
            work_details="The compatibility facade received an unsupported engine decision.",
            artifacts_created=None,
            completed_scope=None,
            remaining_scope=None,
            blockers_found=None,
            validation_notes="Unsupported execution engine decision.",
        )

    except ExecutionEngineRejectedError as exc:
        return ExecutorResult(
            status=CODE_EXECUTION_STATUS_REJECTED,
            artifact_type=None,
            output_snapshot="execution_engine_rejected",
            artifact_id=None,
            work_summary=exc.message,
            work_details="The execution engine deliberately rejected the task before execution.",
            artifacts_created=None,
            completed_scope=None,
            remaining_scope=exc.remaining_scope,
            blockers_found="; ".join(exc.blockers_found) if exc.blockers_found else None,
            validation_notes="; ".join(
                exc.validation_notes
                or ["Execution was rejected at the execution engine boundary."]
            ),
        )

    except ExecutionEngineError as exc:
        return ExecutorResult(
            status=CODE_EXECUTION_STATUS_FAILED,
            artifact_type=None,
            output_snapshot="execution_engine_failed",
            artifact_id=None,
            work_summary=str(exc),
            work_details="The execution engine failed internally.",
            artifacts_created=None,
            completed_scope=None,
            remaining_scope=None,
            blockers_found=None,
            validation_notes="Internal execution engine failure.",
        )

    except Exception as exc:
        return ExecutorResult(
            status=CODE_EXECUTION_STATUS_FAILED,
            artifact_type=None,
            output_snapshot="execution_engine_failed",
            artifact_id=None,
            work_summary=f"Executor failed unexpectedly: {str(exc)}",
            work_details="Unexpected executor failure outside the expected execution engine flow.",
            artifacts_created=None,
            completed_scope=None,
            remaining_scope=None,
            blockers_found=None,
            validation_notes="Unexpected executor failure.",
        )