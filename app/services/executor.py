from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.task import CODE_EXECUTOR, PLANNING_LEVEL_ATOMIC, Task
from app.services.artifacts import create_artifact


class ExecutorServiceError(Exception):
    """Base exception for executor domain errors."""


class ExecutorRejectedError(ExecutorServiceError):
    """Raised when a task is structurally not executable by the executor."""

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
    """Raised when executor logic fails unexpectedly."""

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


def _build_implementation_brief(task: Task) -> str:
    return f"""
Task ID: {task.id}
Title: {task.title}
Type: {task.task_type}
Planning Level: {task.planning_level}
Executor: {task.executor_type}

Description:
{task.description or "No description provided"}

Objective:
{task.objective or "No objective provided"}

Proposed Solution:
{task.proposed_solution or "No proposed solution provided"}

Implementation Notes:
{task.implementation_notes or "No implementation notes provided"}

Implementation Steps:
{task.implementation_steps or "No implementation steps provided"}

Acceptance Criteria:
{task.acceptance_criteria or "No acceptance criteria provided"}

Tests Required:
{task.tests_required or "No tests specified"}

Technical Constraints:
{task.technical_constraints or "No technical constraints specified"}

Out of Scope:
{task.out_of_scope or "No out of scope items specified"}

Definition of Done:
- The task is atomic and executable by the assigned executor
- A concise implementation brief is generated
- The artifact is stored successfully
""".strip()


def _validate_atomic_task(task: Task) -> None:
    if task.planning_level != PLANNING_LEVEL_ATOMIC:
        raise ExecutorRejectedError(
            message="Task is not atomic and cannot be executed by the executor.",
            failure_code="non_atomic_task",
            work_summary="The executor did not start execution because the task is not atomic.",
            work_details=(
                "Validation failed at executor entrypoint. "
                "The task planning_level is different from 'atomic', so it violates the executor contract."
            ),
            blockers_found="Task planning level must be 'atomic' before execution.",
            validation_notes="Rejected because the task is not executable in its current planning stage.",
        )

    if task.executor_type != CODE_EXECUTOR:
        raise ExecutorRejectedError(
            message=f"Executor '{task.executor_type}' is not supported by this executor service.",
            failure_code="unsupported_executor",
            work_summary="The executor rejected the task because the assigned executor is unsupported.",
            work_details=(
                "Validation failed at executor entrypoint. "
                f"The task executor_type is '{task.executor_type}', but this service only supports '{CODE_EXECUTOR}'."
            ),
            blockers_found="A supported concrete executor must be assigned before execution.",
            validation_notes="Rejected because executor assignment is incompatible with this executor service.",
        )

    has_execution_context = any(
        [
            bool(task.description and task.description.strip()),
            bool(task.objective and task.objective.strip()),
            bool(task.implementation_steps and task.implementation_steps.strip()),
        ]
    )

    if not has_execution_context:
        raise ExecutorRejectedError(
            message=(
                "Task was rejected because it does not contain enough execution context. "
                "Atomic tasks must include at least description, objective, or implementation steps."
            ),
            failure_code="missing_execution_context",
            work_summary="The executor rejected the task because the execution context is insufficient.",
            work_details=(
                "Validation failed after checking the task payload. "
                "The task does not provide enough context for safe execution."
            ),
            blockers_found=(
                "Provide at least one of the following fields with meaningful content: "
                "description, objective, implementation_steps."
            ),
            validation_notes="Rejected because the task lacks minimum executable context.",
        )


def _should_return_partial_result(task: Task) -> bool:
    text_sources = [
        task.technical_constraints or "",
        task.implementation_notes or "",
        task.description or "",
    ]
    combined_text = " ".join(text_sources).lower()

    partial_markers = [
        "[partial]",
        "partial",
        "parcial",
        "phase 1 only",
        "fase 1",
        "mock only",
        "solo mock",
    ]

    return any(marker in combined_text for marker in partial_markers)


def execute_atomic_task(db: Session, task: Task) -> ExecutorResult:
    """
    Executes an atomic task using the assigned executor.

    Current contract:
    - always returns a structured execution report
    - may succeed, partially complete, or reject
    - artifact generation is still mocked through implementation_brief
    """
    try:
        _validate_atomic_task(task)

        implementation_brief = _build_implementation_brief(task)

        artifact = create_artifact(
            db=db,
            project_id=task.project_id,
            task_id=task.id,
            artifact_type="implementation_brief",
            content=implementation_brief,
            created_by="executor_agent",
        )

        if _should_return_partial_result(task):
            return ExecutorResult(
                status="partial",
                artifact_type="implementation_brief",
                output_snapshot="implementation_brief_created_partial",
                artifact_id=artifact.id,
                work_summary="The executor produced a usable partial output for the task.",
                work_details=(
                    "The executor generated the implementation brief artifact successfully, "
                    "but the task metadata indicates that only a partial deliverable should be considered complete "
                    "in this mocked execution phase."
                ),
                artifacts_created=f"implementation_brief:{artifact.id}",
                completed_scope=(
                    "A reusable implementation brief was created and stored as an artifact."
                ),
                remaining_scope=(
                    "The task definition still requires follow-up work before meeting the full definition of done."
                ),
                blockers_found=(
                    "The current mocked executor does not finalize the remaining execution scope."
                ),
                validation_notes=(
                    "Marked as partial because the run produced reusable output, "
                    "but the full definition of done was not satisfied."
                ),
            )

        return ExecutorResult(
            status="succeeded",
            artifact_type="implementation_brief",
            output_snapshot="implementation_brief_created",
            artifact_id=artifact.id,
            work_summary="The executor completed the task successfully.",
            work_details=(
                "The executor validated the task, generated the implementation brief, "
                "stored it as an artifact, and considered the mocked execution complete."
            ),
            artifacts_created=f"implementation_brief:{artifact.id}",
            completed_scope=(
                "The implementation brief artifact was created and the mocked execution contract was satisfied."
            ),
            remaining_scope=None,
            blockers_found=None,
            validation_notes=(
                "Marked as succeeded because the mocked executor satisfied its current definition of done."
            ),
        )

    except ExecutorRejectedError:
        raise
    except Exception as exc:
        raise ExecutorInternalError(
            message=f"Executor failed unexpectedly: {str(exc)}",
            failure_code="internal_executor_error",
        ) from exc

        