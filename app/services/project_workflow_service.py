import json

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    PLANNING_LEVEL_REFINED,
    TASK_STATUS_PENDING,
    Task,
)
from app.schemas.execution_plan import ExecutionBatch, ExecutionPlan
from app.schemas.workflow import (
    ProjectWorkflowResult,
    WorkflowIterationSummary,
)
from app.services.artifacts import create_artifact
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
from app.services.project_storage import (
    CODE_DOMAIN,
    ProjectStorageError,
    ProjectStorageService,
)
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


def _has_any_atomic_tasks(db: Session, project_id: int) -> bool:
    return _has_tasks_at_level(db, project_id, PLANNING_LEVEL_ATOMIC)


def _has_atomic_children(db: Session, parent_task_id: int) -> bool:
    child = (
        db.query(Task)
        .filter(
            Task.parent_task_id == parent_task_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
        )
        .first()
    )
    return child is not None


def _has_refined_children(db: Session, parent_task_id: int) -> bool:
    child = (
        db.query(Task)
        .filter(
            Task.parent_task_id == parent_task_id,
            Task.planning_level == PLANNING_LEVEL_REFINED,
        )
        .first()
    )
    return child is not None


def _get_pending_atomic_generation_parents(
    db: Session,
    *,
    project_id: int,
    planning_level: str,
    enable_technical_refinement: bool,
) -> list[Task]:
    candidate_tasks = _get_pending_tasks_at_level(
        db=db,
        project_id=project_id,
        planning_level=planning_level,
    )

    parents: list[Task] = []

    for task in candidate_tasks:
        if _has_atomic_children(db, task.id):
            continue

        if (
            enable_technical_refinement
            and planning_level == PLANNING_LEVEL_HIGH_LEVEL
            and _has_refined_children(db, task.id)
        ):
            continue

        parents.append(task)

    return parents


def _run_planner_if_needed(db: Session, project_id: int) -> bool:
    if _has_tasks_at_level(db, project_id, PLANNING_LEVEL_HIGH_LEVEL):
        return True

    generate_project_plan(db=db, project_id=project_id)
    return True


def _run_optional_technical_refinement_phase(
    db: Session,
    project_id: int,
    *,
    enable_technical_refinement: bool,
) -> bool:
    if not enable_technical_refinement:
        return True

    pending_high_level_tasks = _get_pending_tasks_at_level(
        db=db,
        project_id=project_id,
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
    )

    if not pending_high_level_tasks:
        return True

    for task in pending_high_level_tasks:
        refine_high_level_task(
            db=db,
            project_id=project_id,
            task_id=task.id,
        )

    return True


def _run_atomic_generation_phase(
    db: Session,
    project_id: int,
    *,
    enable_technical_refinement: bool,
) -> bool:
    if enable_technical_refinement:
        parent_levels_in_order = [
            PLANNING_LEVEL_REFINED,
            PLANNING_LEVEL_HIGH_LEVEL,
        ]
    else:
        parent_levels_in_order = [
            PLANNING_LEVEL_HIGH_LEVEL,
            PLANNING_LEVEL_REFINED,
        ]

    processed_any_parent = False

    for planning_level in parent_levels_in_order:
        parents = _get_pending_atomic_generation_parents(
            db=db,
            project_id=project_id,
            planning_level=planning_level,
            enable_technical_refinement=enable_technical_refinement,
        )

        for task in parents:
            generate_atomic_tasks(
                db=db,
                project_id=project_id,
                task_id=task.id,
            )
            processed_any_parent = True

    if processed_any_parent:
        return True

    if _has_any_atomic_tasks(db, project_id):
        return True

    for planning_level in parent_levels_in_order:
        if _has_tasks_at_level(db, project_id, planning_level):
            return True

    return False


def _get_task_or_raise(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise ProjectWorkflowServiceError(f"Task {task_id} not found")
    return task


def _assert_batch_task_is_atomic(
    db: Session,
    *,
    task_id: int,
    batch_id: str | None = None,
    plan_version: int | None = None,
) -> Task:
    task = _get_task_or_raise(db, task_id)

    if task.planning_level != PLANNING_LEVEL_ATOMIC:
        location_parts: list[str] = []
        if batch_id is not None:
            location_parts.append(f"batch '{batch_id}'")
        if plan_version is not None:
            location_parts.append(f"plan version {plan_version}")

        location_suffix = f" in {' / '.join(location_parts)}" if location_parts else ""

        raise ProjectWorkflowServiceError(
            f"Execution plan integrity error{location_suffix}: task {task.id} has planning_level "
            f"'{task.planning_level}'. Only atomic tasks may be executed. Parent tasks must be "
            "resolved deterministically from their children and must never reach the executor."
        )

    return task


def _get_latest_project_artifact_id(
    db: Session,
    *,
    project_id: int,
) -> int:
    latest_artifact_id = (
        db.query(func.max(Artifact.id)).filter(Artifact.project_id == project_id).scalar()
    )
    return int(latest_artifact_id or 0)


def _get_latest_execution_run_for_task(
    db: Session,
    *,
    task_id: int,
) -> ExecutionRun | None:
    return (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )


def _serialize_batch_trace(trace_payload: dict) -> str:
    return json.dumps(trace_payload, ensure_ascii=False, indent=2)


def _persist_workflow_batch_trace(
    db: Session,
    *,
    project_id: int,
    plan: ExecutionPlan,
    batch: ExecutionBatch,
    iteration_number: int,
    task_ids: list[int],
    post_batch_result,
    patched_plan_version: int | None = None,
) -> None:
    task_run_summaries: list[dict] = []

    for task_id in task_ids:
        last_run = _get_latest_execution_run_for_task(db=db, task_id=task_id)
        task_run_summaries.append(
            {
                "task_id": task_id,
                "run_id": last_run.id if last_run else None,
                "run_status": last_run.status if last_run else None,
                "failure_type": last_run.failure_type if last_run else None,
                "failure_code": last_run.failure_code if last_run else None,
                "completed_scope": last_run.completed_scope if last_run else None,
                "remaining_scope": last_run.remaining_scope if last_run else None,
                "validation_notes": last_run.validation_notes if last_run else None,
            }
        )

    payload = {
        "iteration_number": iteration_number,
        "plan_version": plan.plan_version,
        "supersedes_plan_version": plan.supersedes_plan_version,
        "batch_index": batch.batch_index,
        "batch_id": batch.batch_id,
        "checkpoint_id": batch.checkpoint_id,
        "task_ids": list(task_ids),
        "task_run_summaries": task_run_summaries,
        "post_batch_status": getattr(post_batch_result, "status", None),
        "resolved_intent_type": getattr(post_batch_result, "resolved_intent_type", None),
        "resolved_mutation_scope": getattr(
            post_batch_result,
            "resolved_mutation_scope",
            None,
        ),
        "remaining_plan_still_valid": getattr(
            post_batch_result,
            "remaining_plan_still_valid",
            None,
        ),
        "has_new_recovery_tasks": getattr(
            post_batch_result,
            "has_new_recovery_tasks",
            None,
        ),
        "requires_plan_mutation": getattr(
            post_batch_result,
            "requires_plan_mutation",
            None,
        ),
        "requires_all_new_tasks_assigned": getattr(
            post_batch_result,
            "requires_all_new_tasks_assigned",
            None,
        ),
        "can_continue_after_application": getattr(
            post_batch_result,
            "can_continue_after_application",
            None,
        ),
        "should_close_stage": getattr(post_batch_result, "should_close_stage", None),
        "requires_manual_review": getattr(
            post_batch_result,
            "requires_manual_review",
            None,
        ),
        "reopened_finalization": getattr(
            post_batch_result,
            "reopened_finalization",
            None,
        ),
        "finalization_guard_triggered": getattr(
            post_batch_result,
            "finalization_guard_triggered",
            False,
        ),
        "finalization_iteration_count": getattr(
            post_batch_result,
            "finalization_iteration_count",
            0,
        ),
        "decision_signals": getattr(post_batch_result, "decision_signals", []),
        "patched_plan_version": patched_plan_version,
        "notes": getattr(post_batch_result, "notes", None),
    }

    create_artifact(
        db=db,
        project_id=project_id,
        task_id=None,
        artifact_type="workflow_batch_trace",
        content=_serialize_batch_trace(payload),
        created_by="project_workflow_service",
    )


def _execute_batch_tasks_synchronously(
    db: Session,
    batch_task_ids: list[int],
    *,
    batch_id: str | None = None,
    plan_version: int | None = None,
) -> None:
    for task_id in batch_task_ids:
        _assert_batch_task_is_atomic(
            db=db,
            task_id=task_id,
            batch_id=batch_id,
            plan_version=plan_version,
        )

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
    checkpoint_artifact_window_start_exclusive: int,
):
    try:
        return process_batch_after_execution(
            db=db,
            project_id=project_id,
            plan=plan,
            batch_id=batch_id,
            persist_result=True,
            finalization_iteration_count=current_finalization_iteration_count,
            max_finalization_iterations=max_finalization_iterations,
            checkpoint_artifact_window_start_exclusive=checkpoint_artifact_window_start_exclusive,
        )
    except PostBatchServiceError as exc:
        raise ProjectWorkflowServiceError(
            f"Post-batch processing failed for batch '{batch_id}': {str(exc)}"
        ) from exc


def _build_iteration_notes(
    *,
    resulting_status: str,
    resolved_intent_type: str,
    reopened_finalization: bool,
    requires_manual_review: bool,
    should_close_stage: bool,
    used_patched_plan: bool,
    notes: str,
) -> str:
    if resulting_status == "stage_closed" or should_close_stage:
        return "Iteration ended because the stage was closed."

    if requires_manual_review:
        return "Iteration ended because manual review is required."

    if resolved_intent_type == "replan":
        return "Iteration ended because the remaining plan must be replanned."

    if resolved_intent_type == "resequence":
        return "Iteration ended because the remaining plan must be resequenced."

    if resolved_intent_type == "assign":
        if used_patched_plan:
            return "Iteration continued using a patched execution plan after assigning newly created recovery tasks."
        return (
            "Iteration ended because newly created recovery tasks require a patched execution plan."
        )

    if reopened_finalization:
        return "Iteration ended because finalization was reopened."

    return notes or "Iteration completed."


def _run_execution_iteration(
    db: Session,
    project_id: int,
    plan: ExecutionPlan,
    finalization_iteration_count: int,
    max_finalization_iterations: int,
    iteration_number: int,
    previously_completed_batch_ids: set[str] | None = None,
) -> tuple[WorkflowIterationSummary, str, int, ExecutionPlan, bool]:
    starting_plan_version = plan.plan_version
    processed_batch_ids: list[str] = []
    resulting_status = "execution_in_progress"
    current_finalization_iteration_count = finalization_iteration_count
    iteration_requires_replan = False

    current_plan = plan
    current_index = 0
    used_patched_plan = False
    processed_batch_ids_set: set[str] = set()
    previously_completed_batch_ids = set(previously_completed_batch_ids or ())
    last_post_batch_result = None

    while current_index < len(current_plan.execution_batches):
        batch = current_plan.execution_batches[current_index]

        if batch.batch_id in previously_completed_batch_ids:
            current_index += 1
            continue

        if batch.batch_id in processed_batch_ids_set:
            raise ProjectWorkflowServiceError(
                f"Workflow detected an attempt to reprocess batch '{batch.batch_id}' "
                f"in plan version {current_plan.plan_version}. This would create an execution loop."
            )

        checkpoint_artifact_window_start_exclusive = _get_latest_project_artifact_id(
            db=db,
            project_id=project_id,
        )

        _execute_batch_tasks_synchronously(
            db=db,
            batch_task_ids=batch.task_ids,
            batch_id=batch.batch_id,
            plan_version=current_plan.plan_version,
        )

        post_batch_result = _process_batch_after_terminal_tasks(
            db=db,
            project_id=project_id,
            plan=current_plan,
            batch_id=batch.batch_id,
            current_finalization_iteration_count=current_finalization_iteration_count,
            max_finalization_iterations=max_finalization_iterations,
            checkpoint_artifact_window_start_exclusive=checkpoint_artifact_window_start_exclusive,
        )
        last_post_batch_result = post_batch_result

        batch_patched_execution_plan = getattr(post_batch_result, "patched_execution_plan", None)
        batch_patched_plan_version = (
            batch_patched_execution_plan.plan_version
            if batch_patched_execution_plan is not None
            else None
        )

        _persist_workflow_batch_trace(
            db=db,
            project_id=project_id,
            plan=current_plan,
            batch=batch,
            iteration_number=iteration_number,
            task_ids=batch.task_ids,
            post_batch_result=post_batch_result,
            patched_plan_version=batch_patched_plan_version,
        )

        processed_batch_ids.append(batch.batch_id)
        processed_batch_ids_set.add(batch.batch_id)
        current_finalization_iteration_count = getattr(
            post_batch_result,
            "finalization_iteration_count",
            current_finalization_iteration_count,
        )

        resolved_intent_type = post_batch_result.resolved_intent_type
        requires_manual_review = post_batch_result.requires_manual_review
        should_close_stage = post_batch_result.should_close_stage
        patched_execution_plan = post_batch_result.patched_execution_plan

        if requires_manual_review or getattr(
            post_batch_result,
            "finalization_guard_triggered",
            False,
        ):
            resulting_status = "awaiting_manual_review"
            break

        if (
            getattr(post_batch_result, "status", None) == "project_stage_closed"
            or should_close_stage
        ):
            resulting_status = "stage_closed"
            break

        if resolved_intent_type == "replan":
            resulting_status = "execution_in_progress"
            iteration_requires_replan = True
            break

        if (
            patched_execution_plan is not None
            and post_batch_result.can_continue_after_application
            and not requires_manual_review
        ):
            current_plan = patched_execution_plan
            used_patched_plan = True
            resulting_status = "execution_in_progress"
            current_index += 1
            continue

        if resolved_intent_type in {"assign", "resequence"}:
            resulting_status = "execution_in_progress"
            break

        if resolved_intent_type == "continue":
            current_index += 1
            continue

        resulting_status = "awaiting_manual_review"
        break

    ending_plan_version = current_plan.plan_version

    processed_set = set(processed_batch_ids)
    blocked_batch_ids_after_iteration = [
        current_batch.batch_id
        for current_batch in current_plan.execution_batches
        if current_batch.batch_id not in processed_set
        and current_batch.batch_id not in previously_completed_batch_ids
    ]

    if last_post_batch_result is None:
        raise ProjectWorkflowServiceError(
            "Execution iteration finished without producing any post-batch result."
        )

    iteration_summary = WorkflowIterationSummary(
        iteration_number=iteration_number,
        plan_version=current_plan.plan_version,
        starting_plan_version=starting_plan_version,
        ending_plan_version=ending_plan_version,
        batch_ids_processed=processed_batch_ids,
        blocked_batch_ids_after_iteration=blocked_batch_ids_after_iteration,
        resolved_intent_type=last_post_batch_result.resolved_intent_type,
        resolved_mutation_scope=last_post_batch_result.resolved_mutation_scope,
        remaining_plan_still_valid=last_post_batch_result.remaining_plan_still_valid,
        has_new_recovery_tasks=last_post_batch_result.has_new_recovery_tasks,
        requires_plan_mutation=last_post_batch_result.requires_plan_mutation,
        requires_all_new_tasks_assigned=last_post_batch_result.requires_all_new_tasks_assigned,
        can_continue_after_application=last_post_batch_result.can_continue_after_application,
        should_close_stage=last_post_batch_result.should_close_stage,
        requires_manual_review=last_post_batch_result.requires_manual_review,
        reopened_finalization=last_post_batch_result.reopened_finalization,
        used_patched_plan=used_patched_plan,
        decision_signals=last_post_batch_result.decision_signals,
        notes=_build_iteration_notes(
            resulting_status=resulting_status,
            resolved_intent_type=last_post_batch_result.resolved_intent_type,
            reopened_finalization=last_post_batch_result.reopened_finalization,
            requires_manual_review=last_post_batch_result.requires_manual_review,
            should_close_stage=last_post_batch_result.should_close_stage,
            used_patched_plan=used_patched_plan,
            notes=last_post_batch_result.notes,
        ),
    )
    return (
        iteration_summary,
        resulting_status,
        current_finalization_iteration_count,
        current_plan,
        iteration_requires_replan,
    )


def run_project_workflow(
    db: Session,
    project_id: int,
    max_workflow_iterations: int = 5,
    max_finalization_iterations: int = 2,
) -> ProjectWorkflowResult:
    project = _get_project_or_raise(db=db, project_id=project_id)
    enable_technical_refinement = project.enable_technical_refinement

    _bootstrap_project_storage_for_execution(project_id=project_id)

    planning_completed = _run_planner_if_needed(db=db, project_id=project_id)
    refinement_completed = _run_optional_technical_refinement_phase(
        db=db,
        project_id=project_id,
        enable_technical_refinement=enable_technical_refinement,
    )
    atomic_generation_completed = _run_atomic_generation_phase(
        db=db,
        project_id=project_id,
        enable_technical_refinement=enable_technical_refinement,
    )

    if not atomic_generation_completed:
        raise ProjectWorkflowServiceError(
            "Workflow could not produce or detect atomic tasks after planning/decomposition."
        )

    iterations: list[WorkflowIterationSummary] = []
    completed_batches: list[str] = []
    blocked_batches: list[str] = []

    execution_plan_generated = False
    plan_version: int | None = None
    final_stage_closed = False
    manual_review_required = False
    final_status = "execution_in_progress"
    finalization_iteration_count = 0

    active_plan: ExecutionPlan | None = None

    for iteration_number in range(1, max_workflow_iterations + 1):
        if active_plan is None:
            plan = generate_execution_plan(db=db, project_id=project_id)
            persist_execution_plan(
                db=db,
                project_id=project_id,
                plan=plan,
                created_by="project_workflow_service",
            )
            execution_plan_generated = True
            active_plan = plan
        else:
            plan = active_plan

        plan_version = plan.plan_version

        if not plan.execution_batches:
            final_status = "awaiting_manual_review"
            manual_review_required = True
            iterations.append(
                WorkflowIterationSummary(
                    iteration_number=iteration_number,
                    plan_version=plan.plan_version,
                    starting_plan_version=plan.plan_version,
                    ending_plan_version=plan.plan_version,
                    batch_ids_processed=[],
                    blocked_batch_ids_after_iteration=[],
                    resolved_intent_type="manual_review",
                    resolved_mutation_scope="none",
                    remaining_plan_still_valid=True,
                    has_new_recovery_tasks=False,
                    requires_plan_mutation=False,
                    requires_all_new_tasks_assigned=False,
                    can_continue_after_application=False,
                    should_close_stage=False,
                    requires_manual_review=True,
                    reopened_finalization=False,
                    used_patched_plan=False,
                    decision_signals=["empty_execution_plan"],
                    notes=(
                        "Execution plan generation returned no batches. "
                        "Manual review is required because decomposition or sequencing produced no executable work."
                    ),
                )
            )
            break

        (
            iteration_summary,
            resulting_status,
            finalization_iteration_count,
            resulting_plan,
            iteration_requires_replan,
        ) = _run_execution_iteration(
            db=db,
            project_id=project_id,
            plan=plan,
            finalization_iteration_count=finalization_iteration_count,
            max_finalization_iterations=max_finalization_iterations,
            iteration_number=iteration_number,
            previously_completed_batch_ids=set(completed_batches),
        )

        active_plan = resulting_plan

        if iteration_requires_replan:
            active_plan = None
            plan_version = resulting_plan.plan_version
        else:
            plan_version = active_plan.plan_version

        iterations.append(iteration_summary)
        completed_batches.extend(iteration_summary.batch_ids_processed)

        if resulting_status == "stage_closed":
            final_stage_closed = True
            final_status = "stage_closed"
            break

        if iteration_summary.requires_manual_review:
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
            current_plan = active_plan or generate_execution_plan(db=db, project_id=project_id)
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
            "This may be due to evaluator decisions that explicitly require human intervention, "
            "empty sequencing, unrecoverable blocking states, or workflow iteration limits."
        )
    else:
        notes = "Project workflow ended without explicit closure. Technical refinement was " + (
            "enabled." if enable_technical_refinement else "bypassed."
        )

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
