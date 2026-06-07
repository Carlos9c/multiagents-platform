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

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.execution_run import ExecutionRun
from app.models.task import (
    EXECUTION_ENGINE,
    PENDING_ENGINE_ROUTING_EXECUTOR,
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TERMINAL_TASK_STATUSES,
    Task,
    format_acceptance_criteria,
)
from app.schemas.execution_plan import (
    CandidateAtomicTask,
    ExecutionPlanGenerationInput,
    ExecutionSequencingInstructions,
    ExecutionStateSummary,
    ProjectExecutionContext,
)
from app.schemas.project_start import ProjectStartRequest
from app.services.atomic_task_generator import generate_atomic_tasks
from app.services.atomic_task_generator_client import call_atomic_task_generator_model
from app.services.conversation.impact_assessment_agent import (
    BlockedTaskInfo,
    CompletedTaskSummary,
    EnvironmentDependency,
    ImpactAssessmentInput,
    NewWorkBlock,
    TaskContextForReview,
    TaskRevisionSpec,
    assess_impact,
)
from app.services.conversation.language_utils import detect_language_from_text
from app.services.conversation.task_revision_service import (
    TaskRevision,
    apply_task_revisions,
)
from app.services.execution_sequencer_client import call_execution_sequencer_model
from app.services.live_plan_mutation_service import sync_sequence_order_from_plan
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
    tasks_eliminated: int = 0
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

    completed, full_task_tree = _collect_task_context(db, project_id, blocked_task_id)

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
        full_task_tree=full_task_tree,
        language=detect_language_from_text(user_clarification),
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
            project=project,
            blocked_task=blocked_task,
            user_clarification=user_clarification,
            tasks_to_eliminate=assessment.tasks_to_eliminate,
            tasks_to_modify=assessment.tasks_to_modify,
            new_work_blocks=assessment.new_work_blocks,
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
    episode_idx = _get_episode_index(db, blocked_task.project_id)
    _embed_clarification(blocked_task, user_clarification, episode_index=episode_idx)
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
    project,
    blocked_task: Task,
    user_clarification: str,
    tasks_to_eliminate: list[int],
    tasks_to_modify: list[TaskRevisionSpec],
    new_work_blocks: list[NewWorkBlock],
    reasoning: str,
) -> ResumptionResult:
    """Revise affected pending tasks, apply cancellations, create new work, and retry.

    Phase 6 will add: full plan reintegration via sequencer (_reintegrate_plan).
    """
    project_id: int = project.id

    # ── Phase 4: cancel tasks + cascade-up ───────────────────────────────────
    cancelled_ids = _cancel_tasks_and_cascade(db, project_id, tasks_to_eliminate)

    # ── Apply content revisions ───────────────────────────────────────────────
    revisions = [
        TaskRevision(
            task_id=spec.task_id,
            description=spec.new_description,
            objective=spec.new_objective,
            implementation_steps=spec.new_implementation_steps,
            acceptance_criteria=spec.new_acceptance_criteria,
            technical_constraints=spec.new_technical_constraints,
            depends_on_task_titles=spec.new_depends_on_task_titles,
        )
        for spec in tasks_to_modify
    ]

    revision_result = apply_task_revisions(db, revisions, auto_commit=False)

    # ── Phase 5: create new work via atomizer ─────────────────────────────────
    new_atomic_ids = _create_work_from_blocks(db, project, new_work_blocks)
    added_count = len(new_atomic_ids)

    # ── Phase 6: plan reintegration ───────────────────────────────────────────
    depends_changed = any(s.new_depends_on_task_titles is not None for s in tasks_to_modify)
    _reintegrate_plan(
        db,
        project,
        new_atomic_ids=new_atomic_ids,
        cancelled_ids=cancelled_ids,
        depends_changed=depends_changed,
    )

    episode_idx = _get_episode_index(db, project_id)
    _embed_clarification(blocked_task, user_clarification, episode_index=episode_idx)
    blocked_task.status = TASK_STATUS_PENDING
    blocked_task.review_attempts = 0
    db.commit()

    logger.info(
        "moderate_resumption_applied project_id=%s revised=%d added=%d eliminated=%d",
        project_id,
        revision_result.revised_count,
        added_count,
        len(cancelled_ids),
    )

    return ResumptionResult(
        scope="moderate",
        message=(
            f"Cancelled {len(cancelled_ids)} task(s), updated {revision_result.revised_count} "
            f"task(s), and added {added_count} new task(s). "
            "Execution will continue with the revised plan."
        ),
        reasoning=reasoning,
        tasks_modified=revision_result.revised_count,
        tasks_added=added_count,
        tasks_eliminated=len(cancelled_ids),
    )


def _reintegrate_plan(
    db: Session,
    project,
    *,
    new_atomic_ids: list[int],
    cancelled_ids: set[int],
    depends_changed: bool,
) -> None:
    """Reorder pending tasks after a moderate scope change.

    When new atomic tasks were added, invokes the execution sequencer to produce
    a dependency-aware plan and syncs Task.sequence_order from it.

    When only cancellations / dependency updates happened, uses the cheap
    mechanical renumber (_resequence_pending_tasks) — no LLM call needed.
    """
    if new_atomic_ids:
        _resequence_with_sequencer(db, project)
    elif cancelled_ids or depends_changed:
        _resequence_pending_tasks(db, project.id)
        db.flush()
    # else: nothing changed that affects ordering — skip


def _build_pending_candidates(
    db: Session,
    project_id: int,
) -> list[CandidateAtomicTask]:
    """Build CandidateAtomicTask list from all pending atomic tasks in the project."""
    pending = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
            Task.status == TASK_STATUS_PENDING,
        )
        .order_by(Task.sequence_order.asc().nullslast(), Task.id.asc())
        .all()
    )

    if not pending:
        return []

    # Resolve parent titles for context (single extra query)
    parent_ids = {t.parent_task_id for t in pending if t.parent_task_id}
    parent_title: dict[int, str] = {}
    for pid in parent_ids:
        parent_task = db.get(Task, pid)
        if parent_task:
            parent_title[pid] = parent_task.title

    return [
        CandidateAtomicTask(
            task_id=t.id,
            title=t.title,
            description=t.description,
            summary=t.summary,
            objective=t.objective,
            task_type=t.task_type,
            priority=t.priority or "medium",
            planning_level=PLANNING_LEVEL_ATOMIC,
            executor_type=t.executor_type,
            status=t.status,
            parent_task_id=t.parent_task_id,
            parent_high_level_title=(
                parent_title.get(t.parent_task_id) if t.parent_task_id else None
            ),
            implementation_steps=t.implementation_steps,
            acceptance_criteria=format_acceptance_criteria(t.acceptance_criteria),
            estimated_complexity=t.estimated_complexity,
            depends_on_task_titles=t.depends_on_task_titles or [],
            tests_required=t.tests_required,
            technical_constraints=t.technical_constraints,
            out_of_scope=t.out_of_scope,
        )
        for t in pending
    ]


def _resequence_with_sequencer(db: Session, project) -> None:
    """Invoke the execution sequencer to produce a dependency-aware task ordering.

    Called by _reintegrate_plan when new atomic tasks were added. Updates
    Task.sequence_order for all pending atomics based on the plan's batch positions.
    The plan is NOT persisted as an artifact — we only use it for ordering.
    """
    project_id: int = project.id

    candidates = _build_pending_candidates(db, project_id)
    if not candidates:
        logger.warning(
            "resequence_with_sequencer_skipped project_id=%s reason=no_pending_atomics",
            project_id,
        )
        return

    seq_input = ExecutionPlanGenerationInput(
        project_context=ProjectExecutionContext(
            project_id=project_id,
            project_name=project.name,
            project_goal=project.description or project.name,
            current_execution_objective=(
                "Resume execution after user-requested scope change — "
                "reorder all pending tasks respecting dependencies"
            ),
        ),
        candidate_atomic_tasks=candidates,
        execution_state=ExecutionStateSummary(),
        instructions=ExecutionSequencingInstructions(
            goal="Produce a dependency-aware execution order for the revised plan",
            requirements=[
                "Respect all depends_on_task_titles constraints strictly",
                "Group logically independent tasks in the same batch",
                "Place setup/infrastructure tasks before dependent implementation tasks",
            ],
            checkpoint_policy="Insert a checkpoint after each batch",
        ),
    )

    plan = call_execution_sequencer_model(
        seq_input,
        project_id=project_id,
        call_type="resequence",
    )

    all_pending_ids = {c.task_id for c in candidates}
    sync_sequence_order_from_plan(db, plan=plan, task_ids_to_sync=all_pending_ids)
    db.flush()

    logger.info(
        "resequence_with_sequencer_done project_id=%s tasks=%d batches=%d",
        project_id,
        len(all_pending_ids),
        len(plan.execution_batches),
    )


def _cancel_tasks_and_cascade(
    db: Session,
    project_id: int,
    task_ids_to_cancel: list[int],
) -> set[int]:
    """Mark tasks as CANCELLED and cascade up to parents when all children are cancelled.

    Cascade rule: a parent task is cancelled if and only if:
    - ALL of its direct children are CANCELLED
    - AND none of them are COMPLETED (no productive work was committed)

    The cascade iterates upward through the task hierarchy until no more parents
    qualify for cancellation.

    Returns the complete set of cancelled task IDs (direct + all cascaded parents).
    """
    if not task_ids_to_cancel:
        return set()

    cancelled_ids: set[int] = set()
    parents_to_check: set[int] = set()

    # ── Step 1: cancel the directly requested tasks ───────────────────────────
    for task_id in task_ids_to_cancel:
        task = db.get(Task, task_id)
        if task is None or task.project_id != project_id:
            logger.warning(
                "cancel_task_skipped task_id=%s reason=not_found_or_wrong_project",
                task_id,
            )
            continue
        if task.status in TERMINAL_TASK_STATUSES:
            logger.warning(
                "cancel_task_skipped task_id=%s reason=already_terminal status=%s",
                task_id,
                task.status,
            )
            continue
        task.status = TASK_STATUS_CANCELLED
        cancelled_ids.add(task_id)
        logger.info("task_cancelled task_id=%s title=%r", task_id, task.title)
        if task.parent_task_id is not None:
            parents_to_check.add(task.parent_task_id)

    db.flush()

    # ── Step 2: cascade-up — repeat until no more parents qualify ─────────────
    while parents_to_check:
        next_round: set[int] = set()
        for parent_id in parents_to_check:
            parent = db.get(Task, parent_id)
            if parent is None or parent.project_id != project_id:
                continue
            if parent.status in TERMINAL_TASK_STATUSES:
                continue

            children = (
                db.query(Task)
                .filter(
                    Task.parent_task_id == parent_id,
                    Task.project_id == project_id,
                )
                .all()
            )
            if not children:
                continue

            # Cancel parent only when ALL children are CANCELLED (none still pending,
            # none completed — if any work was completed, the parent survives).
            all_cancelled = all(c.status == TASK_STATUS_CANCELLED for c in children)
            if all_cancelled:
                parent.status = TASK_STATUS_CANCELLED
                cancelled_ids.add(parent_id)
                logger.info(
                    "parent_task_cascade_cancelled task_id=%s title=%r children=%d",
                    parent_id,
                    parent.title,
                    len(children),
                )
                if parent.parent_task_id is not None:
                    next_round.add(parent.parent_task_id)

        db.flush()
        parents_to_check = next_round

    return cancelled_ids


def _build_sibling_atomic_summary(db: Session, project_id: int) -> list[dict]:
    """Collect existing pending atomic task titles to pass to the atomizer.

    Prevents the atomizer from generating duplicates of work that is already
    planned for the same project.
    """
    tasks = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
            Task.status == TASK_STATUS_PENDING,
        )
        .all()
    )
    return [{"title": t.title, "task_type": t.task_type} for t in tasks]


def _create_work_from_blocks(
    db: Session,
    project,
    new_work_blocks: list[NewWorkBlock],
) -> list[int]:
    """Materialise new work blocks as atomic Task records via the atomizer.

    Two paths based on NewWorkBlock.planning_level:

    "high_level": Creates a new high_level parent Task in the DB, then calls
        generate_atomic_tasks() to produce atomic children under it. Preserves
        the project's task hierarchy (no orphaned atomics).

    "atomic": Calls call_atomic_task_generator_model() with inline data from the
        block and creates atomic Task records anchored to the specified existing
        parent (block.parent_task_id). The atomizer decides the count (1–12).

    Returns the IDs of all newly created atomic tasks.
    """
    if not new_work_blocks:
        return []

    project_id: int = project.id
    sibling_summary = _build_sibling_atomic_summary(db, project_id)

    # Base sequence_order for appending new tasks (Phase 6 reorders properly)
    max_order: int = (
        db.query(func.max(Task.sequence_order)).filter(Task.project_id == project_id).scalar()
    ) or 0

    new_atomic_ids: list[int] = []

    for block in new_work_blocks:
        if block.planning_level == PLANNING_LEVEL_HIGH_LEVEL:
            ids = _create_high_level_block(
                db=db,
                project=project,
                block=block,
                sibling_summary=sibling_summary,
            )
        else:
            # planning_level == "atomic": inline atomizer, existing parent
            ids = _create_atomic_block(
                db=db,
                project=project,
                block=block,
                sibling_summary=sibling_summary,
                base_sequence_order=max_order,
            )

        new_atomic_ids.extend(ids)
        max_order += len(ids)

        # Keep sibling summary current so the next block avoids duplication
        for tid in ids:
            t = db.get(Task, tid)
            if t is not None:
                sibling_summary.append({"title": t.title, "task_type": t.task_type})

    logger.info(
        "work_blocks_materialised project_id=%s blocks=%d new_atomics=%d",
        project_id,
        len(new_work_blocks),
        len(new_atomic_ids),
    )
    return new_atomic_ids


def _create_high_level_block(
    db: Session,
    project,
    block: NewWorkBlock,
    sibling_summary: list[dict],
) -> list[int]:
    """Create a new high_level parent Task and atomise it via generate_atomic_tasks()."""
    parent = Task(
        project_id=project.id,
        title=block.title,
        description=block.description,
        objective=block.objective,
        proposed_solution=block.proposed_solution,
        acceptance_criteria=block.acceptance_criteria,
        technical_constraints=block.technical_constraints,
        out_of_scope=block.out_of_scope,
        task_type=block.task_type,
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
        status=TASK_STATUS_PENDING,
        depends_on_task_titles=block.depends_on_task_titles,
    )
    db.add(parent)
    db.flush()  # get parent.id before passing to atomizer

    result = generate_atomic_tasks(
        db,
        project_id=project.id,
        task_id=parent.id,
        sibling_atomic_summary=sibling_summary or None,
    )
    atomic_ids: list[int] = result.get("atomic_task_ids", [])
    logger.info(
        "high_level_block_atomised project_id=%s parent_id=%s atomics=%d",
        project.id,
        parent.id,
        len(atomic_ids),
    )
    return atomic_ids


def _create_atomic_block(
    db: Session,
    project,
    block: NewWorkBlock,
    sibling_summary: list[dict],
    base_sequence_order: int,
) -> list[int]:
    """Call the atomizer inline and create atomic Tasks under the existing parent.

    The block's parent_task_id is used as the anchor — all generated atomics
    get parent_task_id=block.parent_task_id, preserving the invariant that every
    atomic task has a parent.
    """
    atomic_output = call_atomic_task_generator_model(
        project_name=project.name,
        project_description=project.description or "",
        parent_task_title=block.title,
        parent_task_description=block.description,
        # Use objective as the summary — the block has no explicit summary field
        parent_task_summary=block.objective,
        parent_task_objective=block.objective,
        parent_task_type=block.task_type,
        # Tell the atomizer this is a parent-level description to be broken into atomics
        parent_task_planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_proposed_solution=block.proposed_solution,
        parent_task_implementation_steps="",
        parent_task_acceptance_criteria=block.acceptance_criteria,
        parent_task_tests_required="",
        parent_task_technical_constraints=block.technical_constraints,
        parent_task_out_of_scope=block.out_of_scope,
        available_executors=[EXECUTION_ENGINE],
        sibling_atomic_summary=sibling_summary or None,
        project_id=project.id,
        parent_task_id=block.parent_task_id,
        call_type="scope_change",  # distinct call_type for supervisor tracing
    )

    created_tasks: list[Task] = []
    for index, atomic in enumerate(atomic_output.atomic_tasks, start=1):
        task = Task(
            project_id=project.id,
            parent_task_id=block.parent_task_id,  # invariant: every atomic has a parent
            title=atomic.title,
            description=atomic.description,
            summary=atomic.summary,
            objective=atomic.objective,
            proposed_solution=atomic.proposed_solution,
            implementation_steps="\n".join(
                f"- {s}" for s in atomic.implementation_steps if s.strip()
            ),
            acceptance_criteria=atomic.acceptance_criteria,
            estimated_complexity=atomic.estimated_complexity,
            depends_on_task_titles=atomic.depends_on_task_titles,
            tests_required="\n".join(f"- {s}" for s in atomic.tests_required if s.strip()),
            technical_constraints=atomic.technical_constraints,
            out_of_scope=atomic.out_of_scope,
            priority=atomic.priority,
            task_type=atomic.task_type,
            verification_level=atomic.verification_level,
            planning_level=PLANNING_LEVEL_ATOMIC,
            executor_type=EXECUTION_ENGINE,
            sequence_order=base_sequence_order + index,
            status=TASK_STATUS_PENDING,
        )
        db.add(task)
        created_tasks.append(task)

    db.flush()  # assigns DB IDs
    atomic_ids = [t.id for t in created_tasks]
    logger.info(
        "atomic_block_materialised project_id=%s parent_id=%s atomics=%d",
        project.id,
        block.parent_task_id,
        len(atomic_ids),
    )
    return atomic_ids


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

    # Set the active conversation to REPLANNING before the long LLM planning call so
    # any concurrent message handler sees the transitional state instead of REVIEWING.
    # This prevents double-processing (review_agent called again on stale context).
    _set_conversation_replanning(db, project_id)

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


def _collect_task_context(
    db: Session,
    project_id: int,
    blocked_task_id: int,
) -> tuple[list[CompletedTaskSummary], list[TaskContextForReview]]:
    """Collect task context for ImpactAssessmentAgent.

    Returns:
        completed: brief list of completed/partial tasks (immutable, for LLM awareness).
        full_task_tree: all non-terminal tasks with hierarchy and dependency info,
            including the blocked task itself (for full cascade context).
    """
    all_tasks = (
        db.query(Task)
        .filter(Task.project_id == project_id)
        .order_by(Task.sequence_order.asc().nullslast())
        .all()
    )

    # Build title lookup for parent resolution
    title_by_id: dict[int, str] = {t.id: t.title for t in all_tasks}

    completed: list[CompletedTaskSummary] = []
    full_task_tree: list[TaskContextForReview] = []

    completed_statuses = {"completed", "partial"}

    for task in all_tasks:
        if task.status in completed_statuses:
            if task.id != blocked_task_id:
                completed.append(CompletedTaskSummary(task_id=task.id, title=task.title))
        elif task.status not in TERMINAL_TASK_STATUSES:
            full_task_tree.append(
                TaskContextForReview(
                    task_id=task.id,
                    title=task.title,
                    description=task.description,
                    planning_level=task.planning_level,
                    parent_task_id=task.parent_task_id,
                    parent_title=(
                        title_by_id.get(task.parent_task_id) if task.parent_task_id else None
                    ),
                    status=task.status,
                    depends_on_task_titles=task.depends_on_task_titles or [],
                    acceptance_criteria=format_acceptance_criteria(task.acceptance_criteria),
                    sequence_order=task.sequence_order,
                )
            )

    return completed, full_task_tree


def _get_episode_index(db: Session, project_id: int) -> int:
    """Return the current review episode attempt count from the active conversation."""
    from app.models.conversation import CONVERSATION_STATUS_ACTIVE, Conversation  # noqa: PLC0415

    conv = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )
    return (conv.review_episode_attempts or 0) if conv else 0


def _embed_clarification(
    task: Task,
    clarification: str,
    *,
    episode_index: int = 0,
) -> None:
    """Append user clarification to task description and to structured clarifications list."""
    existing = task.description or ""
    task.description = existing + _CLARIFICATION_HEADER + clarification.strip()
    task.revised_at = datetime.now(timezone.utc)

    entry: dict = {
        "text": clarification.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "episode_index": episode_index,
    }
    task.clarifications = list(task.clarifications or []) + [entry]


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


def _set_conversation_replanning(db: Session, project_id: int) -> None:
    """Transition the active conversation to REPLANNING before a long LLM planning call.

    This prevents concurrent message handlers from seeing the stale REVIEWING phase
    and attempting to re-run review_agent while ProjectStartService.start() is in flight.
    The phase is committed as part of the surrounding db.commit() in _apply_disruptive().
    The orchestrator will overwrite it with EXECUTING when the resumption tool returns.
    """
    from app.models.conversation import (  # noqa: PLC0415
        CONVERSATION_PHASE_REPLANNING,
        CONVERSATION_STATUS_ACTIVE,
        Conversation,
    )

    active_conv = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )
    if active_conv is not None:
        active_conv.phase = CONVERSATION_PHASE_REPLANNING
        db.add(active_conv)
