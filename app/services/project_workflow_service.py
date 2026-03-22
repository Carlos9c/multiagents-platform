from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    PLANNING_LEVEL_REFINED,
    TASK_STATUS_PENDING,
    Task,
)
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.workflow import (
    ProjectWorkflowResult,
    WorkflowIterationSummary,
)
from app.services.atomic_task_generator import generate_atomic_tasks
from app.services.execution_plan_service import (
    generate_execution_plan,
    persist_execution_plan,
)
from app.services.planner import generate_project_plan
from app.services.post_batch_service import (
    PostBatchServiceError,
    process_batch_after_execution,
)
from app.services.project_storage import CODE_DOMAIN, ProjectStorageError, ProjectStorageService
from app.services.task_execution_service import (
    TaskExecutionServiceError,
    execute_task_sync,
)
from app.services.technical_task_refiner import refine_high_level_task


class ProjectWorkflowServiceError(Exception):
    """Base exception for project workflow orchestration failures."""


def _get_project_or_raise(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise ProjectWorkflowServiceError(f"Project {project_id} not found")
    return project


def _bootstrap_project_storage_for_execution(project_id: int) -> None:
    """
    Ensures the local project storage structure exists before the workflow
    enters any execution-capable phase.

    Current pragmatic decision:
    - bootstrap universal project storage
    - bootstrap code domain storage
    - write/update a storage manifest

    This is done at workflow start so storage failures happen early and
    predictably, instead of appearing mid-execution.
    """
    try:
        storage_service = ProjectStorageService()
        storage_service.ensure_project_storage(project_id)
        storage_service.ensure_domain_storage(project_id, CODE_DOMAIN)
        storage_service.write_project_storage_manifest(
            project_id=project_id,
            enabled_domains=[CODE_DOMAIN],
        )
    except ProjectStorageError as exc:
        raise ProjectWorkflowServiceError(
            f"Failed to bootstrap project storage for project {project_id}: {str(exc)}"
        ) from exc
    except Exception as exc:
        raise ProjectWorkflowServiceError(
            f"Unexpected error while bootstrapping project storage for project {project_id}: {str(exc)}"
        ) from exc


def _has_tasks_at_level(db: Session, project_id: int, planning_level: str) -> bool:
    task = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.planning_level == planning_level,
        )
        .first()
    )
    return task is not None


def _get_pending_tasks_at_level(
    db: Session,
    project_id: int,
    planning_level: str,
) -> list[Task]:
    return (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.planning_level == planning_level,
            Task.status == TASK_STATUS_PENDING,
        )
        .order_by(Task.id.asc())
        .all()
    )


def _run_planner_if_needed(db: Session, project_id: int) -> bool:
    if _has_tasks_at_level(db, project_id, PLANNING_LEVEL_HIGH_LEVEL):
        return True

    generate_project_plan(db=db, project_id=project_id)
    return True


def _run_technical_refinement_phase(db: Session, project_id: int) -> bool:
    pending_high_level_tasks = _get_pending_tasks_at_level(
        db=db,
        project_id=project_id,
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
    )

    if not pending_high_level_tasks:
        return _has_tasks_at_level(db, project_id, PLANNING_LEVEL_REFINED)

    for task in pending_high_level_tasks:
        refine_high_level_task(
            db=db,
            project_id=project_id,
            task_id=task.id,
        )

    return True


def _run_atomic_generation_phase(db: Session, project_id: int) -> bool:
    pending_refined_tasks = _get_pending_tasks_at_level(
        db=db,
        project_id=project_id,
        planning_level=PLANNING_LEVEL_REFINED,
    )

    if not pending_refined_tasks:
        return _has_tasks_at_level(db, project_id, PLANNING_LEVEL_ATOMIC)

    for task in pending_refined_tasks:
        generate_atomic_tasks(
            db=db,
            project_id=project_id,
            task_id=task.id,
        )

    return True


def _execute_batch_tasks_synchronously(
    db: Session,
    batch_task_ids: list[int],
) -> None:
    """
    Executes each task in the batch synchronously.

    Important semantic contract:
    execute_task_sync(...) performs the full local task pipeline:
    - execution
    - validation
    - final task-state consolidation

    Therefore, when this function returns successfully for a task, that task
    must already be in a terminal state suitable for post-batch processing.
    """
    for task_id in batch_task_ids:
        try:
            result = execute_task_sync(db=db, task_id=task_id)

            if result.final_task_status is None:
                raise ProjectWorkflowServiceError(
                    f"Task {task_id} finished execution without a final consolidated task status."
                )

        except TaskExecutionServiceError as exc:
            raise ProjectWorkflowServiceError(
                f"Failed to execute and validate task {task_id} synchronously: {str(exc)}"
            ) from exc


def _process_batch_after_terminal_tasks(
    db: Session,
    project_id: int,
    plan: ExecutionPlan,
    batch_id: str,
    current_finalization_iteration_count: int,
    max_finalization_iterations: int,
):
    """
    Runs post-batch processing only after the batch tasks have completed
    execution+validation and are in terminal task states.
    """
    try:
        return process_batch_after_execution(
            db=db,
            project_id=project_id,
            plan=plan,
            batch_id=batch_id,
            persist_result=True,
            finalization_iteration_count=current_finalization_iteration_count,
            max_finalization_iterations=max_finalization_iterations,
        )
    except PostBatchServiceError as exc:
        raise ProjectWorkflowServiceError(
            f"Post-batch processing failed for batch '{batch_id}': {str(exc)}"
        ) from exc


def _run_execution_iteration(
    db: Session,
    project_id: int,
    plan: ExecutionPlan,
    finalization_iteration_count: int,
    max_finalization_iterations: int,
    iteration_number: int,
) -> tuple[WorkflowIterationSummary, str, int]:
    processed_batch_ids: list[str] = []
    blocked = False
    reopened_finalization = False
    manual_review_required = False
    resulting_status = "execution_in_progress"
    current_finalization_iteration_count = finalization_iteration_count

    for batch in plan.execution_batches:
        _execute_batch_tasks_synchronously(
            db=db,
            batch_task_ids=batch.task_ids,
        )

        post_batch_result = _process_batch_after_terminal_tasks(
            db=db,
            project_id=project_id,
            plan=plan,
            batch_id=batch.batch_id,
            current_finalization_iteration_count=current_finalization_iteration_count,
            max_finalization_iterations=max_finalization_iterations,
        )

        processed_batch_ids.append(batch.batch_id)
        current_finalization_iteration_count = post_batch_result.finalization_iteration_count

        if post_batch_result.requires_manual_review or post_batch_result.finalization_guard_triggered:
            manual_review_required = True
            blocked = True
            resulting_status = "awaiting_manual_review"
            break

        if post_batch_result.status == "finalization_reopened":
            reopened_finalization = True
            blocked = True
            resulting_status = "execution_in_progress"
            break

        if post_batch_result.requires_replanning:
            blocked = True
            resulting_status = "awaiting_manual_review"
            manual_review_required = True
            break

        if post_batch_result.requires_resequencing:
            blocked = True
            reopened_finalization = True
            resulting_status = "execution_in_progress"
            break

        if post_batch_result.status == "project_stage_closed":
            resulting_status = "stage_closed"
            blocked = False
            break

        if not post_batch_result.continue_execution:
            blocked = True
            resulting_status = "awaiting_manual_review"
            manual_review_required = True
            break

    iteration_summary = WorkflowIterationSummary(
        iteration_number=iteration_number,
        plan_version=plan.plan_version,
        batch_ids_processed=processed_batch_ids,
        reopened_finalization=reopened_finalization,
        manual_review_required=manual_review_required,
        notes=(
            "Iteration ended because the stage was closed."
            if resulting_status == "stage_closed"
            else "Iteration ended because a new sequence or manual review is required."
            if blocked
            else "Iteration completed."
        ),
    )

    return iteration_summary, resulting_status, current_finalization_iteration_count


def run_project_workflow(
    db: Session,
    project_id: int,
    max_workflow_iterations: int = 5,
    max_finalization_iterations: int = 2,
) -> ProjectWorkflowResult:
    _get_project_or_raise(db=db, project_id=project_id)
    _bootstrap_project_storage_for_execution(project_id=project_id)

    planning_completed = _run_planner_if_needed(db=db, project_id=project_id)
    refinement_completed = _run_technical_refinement_phase(db=db, project_id=project_id)
    atomic_generation_completed = _run_atomic_generation_phase(db=db, project_id=project_id)

    iterations: list[WorkflowIterationSummary] = []
    completed_batches: list[str] = []
    blocked_batches: list[str] = []

    execution_plan_generated = False
    plan_version: int | None = None
    final_stage_closed = False
    manual_review_required = False
    final_status = "execution_in_progress"
    finalization_iteration_count = 0

    for iteration_number in range(1, max_workflow_iterations + 1):
        plan = generate_execution_plan(db=db, project_id=project_id)
        persist_execution_plan(
            db=db,
            project_id=project_id,
            plan=plan,
            created_by="project_workflow_service",
        )

        execution_plan_generated = True
        plan_version = plan.plan_version

        iteration_summary, resulting_status, finalization_iteration_count = _run_execution_iteration(
            db=db,
            project_id=project_id,
            plan=plan,
            finalization_iteration_count=finalization_iteration_count,
            max_finalization_iterations=max_finalization_iterations,
            iteration_number=iteration_number,
        )

        iterations.append(iteration_summary)
        completed_batches.extend(iteration_summary.batch_ids_processed)

        if resulting_status == "stage_closed":
            final_stage_closed = True
            final_status = "stage_closed"
            break

        if iteration_summary.manual_review_required:
            manual_review_required = True
            final_status = "awaiting_manual_review"
            break

        if iteration_summary.reopened_finalization:
            final_status = "execution_in_progress"
            continue

        final_status = resulting_status

    else:
        manual_review_required = True
        final_status = "awaiting_manual_review"

    if execution_plan_generated:
        try:
            current_plan = generate_execution_plan(db=db, project_id=project_id)
            processed = set(completed_batches)
            blocked_batches = [
                batch.batch_id
                for batch in current_plan.execution_batches
                if batch.batch_id not in processed
            ]
        except Exception:
            blocked_batches = []

    if final_stage_closed:
        notes = "Project workflow completed and the current stage was closed by the evaluator."
    elif manual_review_required:
        notes = (
            "Project workflow stopped awaiting manual review. "
            "This may be due to recovery/evaluation decisions or workflow iteration limits."
        )
    else:
        notes = "Project workflow ended without explicit closure."

    return ProjectWorkflowResult(
        project_id=project_id,
        status=final_status,
        planning_completed=planning_completed,
        refinement_completed=refinement_completed,
        atomic_generation_completed=atomic_generation_completed,
        execution_plan_generated=execution_plan_generated,
        plan_version=plan_version,
        completed_batches=completed_batches,
        blocked_batches=blocked_batches,
        iterations=iterations,
        manual_review_required=manual_review_required,
        final_stage_closed=final_stage_closed,
        notes=notes,
    )