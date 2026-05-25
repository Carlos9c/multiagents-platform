"""Resumption Service.

Orchestrates recovery after a manual_review episode based on user clarification.
Applies one of three tiers (narrow / moderate / disruptive) determined by the
Impact Assessment Agent and leaves the project ready for execution to continue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.models.execution_run import ExecutionRun
from app.models.task import (
    EXECUTION_ENGINE,
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.schemas.project_start import ProjectStartRequest
from app.services.conversation.impact_assessment_agent import (
    BlockedTaskInfo,
    CompletedTaskSummary,
    EnvironmentDependency,
    ImpactAssessmentInput,
    NewTaskSpec,
    PendingTaskSummary,
    TaskRevisionSpec,
    assess_impact,
)
from app.services.conversation.task_revision_service import (
    TaskRevision,
    apply_task_revisions,
)
from app.services.project_start_service import ProjectStartService
from app.services.tasks import mark_tasks_superseded

logger = logging.getLogger(__name__)

_CLARIFICATION_HEADER = "\n\n--- USER CLARIFICATION (post-review) ---\n"


# ── Result contract ───────────────────────────────────────────────────────────


@dataclass
class ResumptionResult:
    scope: Literal["narrow", "moderate", "disruptive"]
    message: str
    reasoning: str
    tasks_modified: int = 0
    tasks_added: int = 0
    tasks_superseded: int = 0


class ResumptionError(Exception):
    pass


# ── Public API ────────────────────────────────────────────────────────────────


def resume_after_review(
    db: Session,
    *,
    project_id: int,
    blocked_task_id: int,
    user_clarification: str,
    updated_project_goal: str | None = None,
) -> ResumptionResult:
    """
    Resume a project paused at a manual_review episode.

    1. Fetches project state and runs Impact Assessment.
    2. Applies the appropriate tier action.
    3. Returns a ResumptionResult the conversational agent can relay to the user.
    """
    blocked_task = db.get(Task, blocked_task_id)
    if blocked_task is None:
        raise ResumptionError(f"Task {blocked_task_id} not found")
    # Accept awaiting_review (normal path) and failed/partial (when the validator marks the
    # task FAILED instead of AWAITING_REVIEW but manual review was still required).
    _RESUMABLE_STATUSES = {TASK_STATUS_AWAITING_REVIEW, TASK_STATUS_FAILED, TASK_STATUS_PARTIAL}
    if blocked_task.status not in _RESUMABLE_STATUSES:
        raise ResumptionError(
            f"Task {blocked_task_id} cannot be resumed (status={blocked_task.status}). "
            f"Expected one of: {', '.join(sorted(_RESUMABLE_STATUSES))}"
        )

    from app.models.project import Project  # local import to avoid circular

    project = db.get(Project, project_id)
    if project is None:
        raise ResumptionError(f"Project {project_id} not found")

    effective_goal = updated_project_goal or project.description or project.name

    completed, pending = _collect_task_summaries(db, project_id, blocked_task_id)

    validation_notes = _get_validation_notes(db, blocked_task_id)

    assessment_input = ImpactAssessmentInput(
        project_goal=effective_goal,
        user_clarification=user_clarification,
        blocked_task=BlockedTaskInfo(
            task_id=blocked_task.id,
            title=blocked_task.title,
            description=blocked_task.description,
            validation_notes=validation_notes,
        ),
        completed_tasks=completed,
        pending_tasks=pending,
    )

    assessment = assess_impact(assessment_input)

    if assessment.environment_changes:
        _apply_environment_delta(db, project, assessment.environment_changes)

    logger.info(
        "resumption_scope_determined project_id=%s blocked_task_id=%s scope=%s",
        project_id,
        blocked_task_id,
        assessment.change_scope,
    )

    if assessment.change_scope == "narrow":
        return _apply_narrow(
            db=db,
            blocked_task=blocked_task,
            user_clarification=user_clarification,
            reasoning=assessment.reasoning,
        )

    if assessment.change_scope == "moderate":
        return _apply_moderate(
            db=db,
            project_id=project_id,
            blocked_task=blocked_task,
            user_clarification=user_clarification,
            tasks_to_modify=assessment.tasks_to_modify,
            tasks_to_add=assessment.tasks_to_add,
            resequence_needed=assessment.resequence_needed,
            reasoning=assessment.reasoning,
        )

    # disruptive
    return _apply_disruptive(
        db=db,
        project_id=project_id,
        project=project,
        blocked_task_id=blocked_task_id,
        updated_goal=effective_goal,
        reasoning=assessment.reasoning,
    )


# ── Narrow ────────────────────────────────────────────────────────────────────


def _apply_narrow(
    db: Session,
    *,
    blocked_task: Task,
    user_clarification: str,
    reasoning: str,
) -> ResumptionResult:
    """Enrich the blocked task's context and reset it to PENDING for retry."""
    _embed_clarification(blocked_task, user_clarification)
    blocked_task.status = TASK_STATUS_PENDING
    blocked_task.review_attempts = 0
    db.commit()

    logger.info("narrow_resumption_applied task_id=%s", blocked_task.id)

    return ResumptionResult(
        scope="narrow",
        message=(
            "The clarification has been applied to the task. "
            "Execution will retry with the updated context."
        ),
        reasoning=reasoning,
    )


# ── Moderate ──────────────────────────────────────────────────────────────────


def _apply_moderate(
    db: Session,
    *,
    project_id: int,
    blocked_task: Task,
    user_clarification: str,
    tasks_to_modify: list[TaskRevisionSpec],
    tasks_to_add: list[NewTaskSpec],
    resequence_needed: bool,
    reasoning: str,
) -> ResumptionResult:
    """Revise affected pending tasks, insert new ones, resequence if needed, then retry."""
    revisions = [
        TaskRevision(
            task_id=spec.task_id,
            description=spec.new_description,
            objective=spec.new_objective,
            implementation_steps=spec.new_implementation_steps,
            acceptance_criteria=spec.new_acceptance_criteria,
            technical_constraints=spec.new_technical_constraints,
        )
        for spec in tasks_to_modify
    ]

    revision_result = apply_task_revisions(db, revisions, auto_commit=False)

    added_count = _insert_new_tasks(db, project_id, blocked_task, tasks_to_add)

    if resequence_needed:
        _resequence_pending_tasks(db, project_id)

    _embed_clarification(blocked_task, user_clarification)
    blocked_task.status = TASK_STATUS_PENDING
    blocked_task.review_attempts = 0
    db.commit()

    logger.info(
        "moderate_resumption_applied project_id=%s revised=%d added=%d resequenced=%s",
        project_id,
        revision_result.revised_count,
        added_count,
        resequence_needed,
    )

    return ResumptionResult(
        scope="moderate",
        message=(
            f"Updated {revision_result.revised_count} task(s) and added {added_count} new task(s). "
            "Execution will continue with the revised plan."
        ),
        reasoning=reasoning,
        tasks_modified=revision_result.revised_count,
        tasks_added=added_count,
    )


def _insert_new_tasks(
    db: Session,
    project_id: int,
    blocked_task: Task,
    specs: list[NewTaskSpec],
) -> int:
    if not specs:
        return 0

    parent_task_id = blocked_task.parent_task_id

    for spec in specs:
        ref_order = _resolve_insert_sequence_order(db, project_id, spec.insert_after_task_id)
        _shift_sequence_orders_above(db, project_id, ref_order)

        task = Task(
            project_id=project_id,
            parent_task_id=parent_task_id,
            title=spec.title,
            description=spec.description,
            objective=spec.objective,
            proposed_solution=spec.proposed_solution,
            implementation_steps=spec.implementation_steps,
            acceptance_criteria=spec.acceptance_criteria,
            technical_constraints=spec.technical_constraints,
            planning_level=PLANNING_LEVEL_ATOMIC,
            executor_type=EXECUTION_ENGINE,
            sequence_order=ref_order + 1,
            status=TASK_STATUS_PENDING,
        )
        db.add(task)

    db.flush()
    return len(specs)


def _resolve_insert_sequence_order(
    db: Session,
    project_id: int,
    insert_after_task_id: int | None,
) -> int:
    if insert_after_task_id is None:
        max_order = (
            db.query(Task.sequence_order)
            .filter(Task.project_id == project_id, Task.sequence_order.isnot(None))
            .order_by(Task.sequence_order.desc())
            .scalar()
        )
        return max_order or 0

    ref_task = db.get(Task, insert_after_task_id)
    if ref_task is None or ref_task.sequence_order is None:
        max_order = (
            db.query(Task.sequence_order)
            .filter(Task.project_id == project_id, Task.sequence_order.isnot(None))
            .order_by(Task.sequence_order.desc())
            .scalar()
        )
        return max_order or 0

    return ref_task.sequence_order


def _shift_sequence_orders_above(db: Session, project_id: int, ref_order: int) -> None:
    tasks_to_shift = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.sequence_order > ref_order,
            Task.status == TASK_STATUS_PENDING,
        )
        .all()
    )
    for task in tasks_to_shift:
        task.sequence_order = (task.sequence_order or 0) + 1


def _resequence_pending_tasks(db: Session, project_id: int) -> None:
    """Re-number sequence_order for all pending tasks to close gaps after insertions."""
    pending = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.status == TASK_STATUS_PENDING,
            Task.sequence_order.isnot(None),
        )
        .order_by(Task.sequence_order.asc())
        .all()
    )
    for new_order, task in enumerate(pending, start=1):
        task.sequence_order = new_order


# ── Disruptive ────────────────────────────────────────────────────────────────


def _apply_disruptive(
    db: Session,
    *,
    project_id: int,
    project,
    blocked_task_id: int,
    updated_goal: str,
    reasoning: str,
) -> ResumptionResult:
    """
    Mark all non-terminal tasks as superseded, update the project goal, and
    trigger a full evolutionary replanning cycle using the existing source_dir.
    """
    non_terminal_ids = [
        row[0]
        for row in db.query(Task.id)
        .filter(
            Task.project_id == project_id,
            Task.status.notin_(TERMINAL_TASK_STATUSES),
        )
        .all()
    ]

    superseded_count = mark_tasks_superseded(db, non_terminal_ids, auto_commit=False)

    if updated_goal and updated_goal != (project.description or ""):
        project.description = updated_goal
        db.add(project)

    db.commit()

    start_service = ProjectStartService()
    start_service.start(
        db,
        ProjectStartRequest(
            project_id=project_id,
            use_existing_source=True,
            description=updated_goal,
        ),
    )

    logger.info(
        "disruptive_resumption_applied project_id=%s superseded=%d",
        project_id,
        superseded_count,
    )

    return ResumptionResult(
        scope="disruptive",
        message=(
            "The project direction has changed significantly. All pending work has been "
            "superseded and a new plan has been generated from the current codebase state."
        ),
        reasoning=reasoning,
        tasks_superseded=superseded_count,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _collect_task_summaries(
    db: Session,
    project_id: int,
    blocked_task_id: int,
) -> tuple[list[CompletedTaskSummary], list[PendingTaskSummary]]:
    all_tasks = (
        db.query(Task)
        .filter(Task.project_id == project_id, Task.planning_level == PLANNING_LEVEL_ATOMIC)
        .order_by(Task.sequence_order.asc().nullslast())
        .all()
    )

    completed: list[CompletedTaskSummary] = []
    pending: list[PendingTaskSummary] = []

    completed_statuses = {"completed", "partial"}

    for task in all_tasks:
        if task.id == blocked_task_id:
            continue
        if task.status in completed_statuses:
            completed.append(CompletedTaskSummary(task_id=task.id, title=task.title))
        elif task.status == TASK_STATUS_PENDING:
            pending.append(
                PendingTaskSummary(
                    task_id=task.id,
                    sequence_order=task.sequence_order,
                    title=task.title,
                    description=task.description,
                )
            )

    return completed, pending


def _embed_clarification(task: Task, clarification: str) -> None:
    """Append the user clarification to the task description so the engine sees it."""
    existing = task.description or ""
    task.description = existing + _CLARIFICATION_HEADER + clarification.strip()
    task.revised_at = datetime.now(timezone.utc)


def _get_validation_notes(db: Session, task_id: int) -> str | None:
    """Build a validation_notes string from the latest ExecutionRun for this task."""
    last_run = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )
    if last_run is None:
        return None
    parts = [
        last_run.validation_notes,
        last_run.blockers_found,
        last_run.error_message,
    ]
    combined = " | ".join(p for p in parts if p)
    return combined or None


def _apply_environment_delta(
    db: Session,
    project: object,
    env_changes: list[EnvironmentDependency],
) -> None:
    """
    Incrementally install new packages into the active runtime environment.

    Uses EnvironmentManager for Level A (app packages) and Level B (system
    packages) changes — installs into the running container without teardown.

    Full teardown + bootstrap is only needed for Level C (runtime type change),
    which is always a disruptive ImpactAssessmentAgent scope change and is
    handled at the project-restart level, not here.

    On version conflict with version_strictness="exact_only": raises ResumptionError
    so the conversation layer can surface the blocker to the user.
    """
    from app.services.environment.contracts import RuntimeSpec
    from app.services.environment.manager import EnvironmentManager
    from app.services.environment.manager_contracts import EnvironmentManagerRequest, PackageRequest

    project_id: int = project.id  # type: ignore[attr-defined]

    if not getattr(project, "runtime_spec", None):
        logger.warning(
            "env_delta_skipped project_id=%s reason=no_runtime_spec",
            project_id,
        )
        return

    try:
        spec = RuntimeSpec.model_validate_json(project.runtime_spec)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning(
            "env_delta_skipped project_id=%s reason=parse_error error=%s",
            project_id,
            exc,
        )
        return

    # Deduplicate: only install packages that are not already in the manifest
    existing_names = {d.name.lower() for d in spec.dependencies}
    packages_to_install: list[PackageRequest] = []
    for dep in env_changes:
        if dep.package_name.lower() not in existing_names:
            packages_to_install.append(
                PackageRequest(
                    name=dep.package_name,
                    version=dep.version_constraint or None,
                    package_type="app",
                )
            )

    if not packages_to_install:
        return  # nothing new to install

    # Determine the most restrictive version_strictness across all changes
    strictness_rank = {"exact_only": 2, "preferred": 1, "any_compatible": 0}
    max_strictness = max(
        (getattr(dep, "version_strictness", "any_compatible") for dep in env_changes),
        key=lambda s: strictness_rank.get(s, 0),
        default="any_compatible",
    )

    workspace_path = getattr(project, "workspace_path", "") or ""

    mgr_request = EnvironmentManagerRequest(
        packages=packages_to_install,
        version_constraint=max_strictness,
        project_id=project_id,
        workspace_path=workspace_path,
        context_description="resumption_service environment delta",
    )

    manager = EnvironmentManager()
    output = manager.install(request=mgr_request, spec=spec)

    added_names = [p.name for p in output.installed_packages]

    if output.status in ("needs_user_input", "failed", "rolled_back"):
        raise ResumptionError(
            f"Failed to apply environment changes for project {project_id}: "
            f"{output.blocker_message or output.status}. "
            f"Conflicts: {output.dependency_conflicts}"
        )

    # Persist updated RuntimeSpec if install succeeded (fully or partially)
    if output.updated_runtime_spec_json:
        project.runtime_spec = output.updated_runtime_spec_json  # type: ignore[attr-defined]
        db.add(project)  # type: ignore[arg-type]
        db.flush()

    logger.info(
        "env_delta_applied project_id=%s status=%s added=%s conflicts=%s",
        project_id,
        output.status,
        added_names,
        output.dependency_conflicts,
    )
