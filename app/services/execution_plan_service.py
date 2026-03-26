import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_AWAITING_VALIDATION,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    Task,
)
from app.schemas.execution_plan import (
    CandidateAtomicTask,
    CheckpointDefinition,
    CompletedTaskSummary,
    ExecutionBatch,
    ExecutionPlan,
    ExecutionPlanGenerationInput,
    ExecutionSequencingInstructions,
    ExecutionStateSummary,
    ProjectExecutionContext,
    RelevantArtifactSummary,
    UnfinishedTaskSummary,
)
from app.services.artifacts import create_artifact
from app.services.execution_sequencer_client import call_execution_sequencer_model


class ExecutionPlanServiceError(Exception):
    """Base exception for execution plan service errors."""


def _serialize_execution_plan(plan: ExecutionPlan) -> str:
    return json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _build_project_context(project: Project) -> ProjectExecutionContext:
    return ProjectExecutionContext(
        project_id=project.id,
        project_name=project.name,
        project_goal=project.description or project.name,
        project_summary=project.description,
        current_execution_objective=(
            "Sequence the active pending atomic tasks to maximize safe progress, "
            "surface dependencies, enforce batch checkpoints, and guarantee stage closure evaluation."
        ),
    )


def _build_candidate_atomic_task(task: Task, parent_task: Task | None) -> CandidateAtomicTask:
    parent_refined_title = None
    parent_high_level_title = None

    if parent_task:
        if parent_task.planning_level == "refined":
            parent_refined_title = parent_task.title
            grandparent = parent_task.parent_task
            if grandparent:
                parent_high_level_title = grandparent.title
        elif parent_task.planning_level == "high_level":
            parent_high_level_title = parent_task.title

    return CandidateAtomicTask(
        task_id=task.id,
        title=task.title,
        description=task.description,
        summary=task.summary,
        objective=task.objective,
        task_type=task.task_type,
        priority=task.priority,
        planning_level=task.planning_level,
        executor_type=task.executor_type,
        status=task.status,
        parent_task_id=task.parent_task_id,
        parent_refined_title=parent_refined_title,
        parent_high_level_title=parent_high_level_title,
        implementation_steps=task.implementation_steps,
        acceptance_criteria=task.acceptance_criteria,
        tests_required=task.tests_required,
        technical_constraints=task.technical_constraints,
        out_of_scope=task.out_of_scope,
    )


def _build_execution_state_summary(
    db: Session,
    project_id: int,
    limit_artifacts: int = 25,
) -> ExecutionStateSummary:
    completed_tasks = (
        db.query(Task)
        .filter(Task.project_id == project_id, Task.status == TASK_STATUS_COMPLETED)
        .order_by(Task.id.asc())
        .all()
    )

    unfinished_tasks = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.status.in_(
                [
                    TASK_STATUS_PENDING,
                    TASK_STATUS_RUNNING,
                    TASK_STATUS_AWAITING_VALIDATION,
                    TASK_STATUS_PARTIAL,
                    TASK_STATUS_FAILED,
                ]
            ),
        )
        .order_by(Task.id.asc())
        .all()
    )

    recent_artifacts = (
        db.query(Artifact)
        .filter(Artifact.project_id == project_id)
        .order_by(Artifact.id.desc())
        .limit(limit_artifacts)
        .all()
    )

    completed_summaries: list[CompletedTaskSummary] = []
    for task in completed_tasks:
        last_run = (
            db.query(ExecutionRun)
            .filter(ExecutionRun.task_id == task.id)
            .order_by(ExecutionRun.id.desc())
            .first()
        )
        completed_summaries.append(
            CompletedTaskSummary(
                task_id=task.id,
                title=task.title,
                status=task.status,
                completed_scope=last_run.completed_scope if last_run else None,
                artifacts_created=last_run.artifacts_created if last_run else None,
                validation_notes=last_run.validation_notes if last_run else None,
            )
        )

    unfinished_summaries: list[UnfinishedTaskSummary] = []
    for task in unfinished_tasks:
        last_run = (
            db.query(ExecutionRun)
            .filter(ExecutionRun.task_id == task.id)
            .order_by(ExecutionRun.id.desc())
            .first()
        )
        unfinished_summaries.append(
            UnfinishedTaskSummary(
                task_id=task.id,
                title=task.title,
                task_status=task.status,
                last_run_status=last_run.status if last_run else None,
                failure_type=last_run.failure_type if last_run else None,
                failure_code=last_run.failure_code if last_run else None,
                completed_scope=last_run.completed_scope if last_run else None,
                remaining_scope=last_run.remaining_scope if last_run else None,
                blockers_found=last_run.blockers_found if last_run else None,
            )
        )

    artifact_summaries = [
        RelevantArtifactSummary(
            artifact_id=artifact.id,
            artifact_type=artifact.artifact_type,
            task_id=artifact.task_id,
            summary=(
                artifact.content[:500] + "..."
                if artifact.content and len(artifact.content) > 500
                else (artifact.content or "")
            ),
        )
        for artifact in recent_artifacts
    ]

    return ExecutionStateSummary(
        completed_tasks=completed_summaries,
        unfinished_tasks=unfinished_summaries,
        relevant_artifacts=artifact_summaries,
    )


def _build_batch_id(plan_version: int, batch_index: int) -> str:
    return f"plan_{plan_version}_batch_{batch_index}"


def _build_batch_internal_id(plan_version: int, batch_index: int) -> str:
    return f"{plan_version}_{batch_index}"


def _build_checkpoint_id(plan_version: int, batch_index: int) -> str:
    return f"checkpoint_plan_{plan_version}_batch_{batch_index}"


def _resolve_generated_plan_version(project: Project, has_persisted_plan: bool) -> int:
    if has_persisted_plan:
        return project.plan_version + 1
    return project.plan_version


def _normalize_execution_plan(
    *,
    raw_plan: ExecutionPlan,
    plan_version: int,
    supersedes_plan_version: int | None,
) -> ExecutionPlan:
    normalized_batches: list[ExecutionBatch] = []
    normalized_checkpoints: list[CheckpointDefinition] = []

    if not raw_plan.execution_batches:
        raise ExecutionPlanServiceError(
            "Execution plan generation returned no execution batches."
        )

    source_checkpoints_by_id = {
        checkpoint.checkpoint_id: checkpoint for checkpoint in raw_plan.checkpoints
    }
    total_batches = len(raw_plan.execution_batches)

    for batch_index, raw_batch in enumerate(raw_plan.execution_batches, start=1):
        batch_id = _build_batch_id(plan_version=plan_version, batch_index=batch_index)
        checkpoint_id = _build_checkpoint_id(
            plan_version=plan_version,
            batch_index=batch_index,
        )

        source_checkpoint = source_checkpoints_by_id.get(raw_batch.checkpoint_id)

        evaluation_focus = (
            list(source_checkpoint.evaluation_focus)
            if source_checkpoint is not None
            else ["functional_coverage"]
        )
        if batch_index == total_batches and "stage_closure" not in evaluation_focus:
            evaluation_focus.append("stage_closure")

        checkpoint_reason = (
            source_checkpoint.reason
            if source_checkpoint is not None and source_checkpoint.reason
            else raw_batch.checkpoint_reason
        )

        normalized_batch = ExecutionBatch(
            batch_internal_id=_build_batch_internal_id(
                plan_version=plan_version,
                batch_index=batch_index,
            ),
            batch_id=batch_id,
            batch_index=batch_index,
            plan_version=plan_version,
            name=f"Plan {plan_version} · Batch {batch_index}",
            goal=raw_batch.goal,
            task_ids=list(raw_batch.task_ids),
            entry_conditions=list(raw_batch.entry_conditions),
            expected_outputs=list(raw_batch.expected_outputs),
            risk_level=raw_batch.risk_level,
            checkpoint_after=True,
            checkpoint_id=checkpoint_id,
            checkpoint_reason=checkpoint_reason,
        )
        normalized_batches.append(normalized_batch)

        normalized_checkpoint = CheckpointDefinition(
            checkpoint_id=checkpoint_id,
            name=(
                source_checkpoint.name
                if source_checkpoint is not None and source_checkpoint.name
                else f"Checkpoint {batch_index}"
            ),
            reason=checkpoint_reason,
            after_batch_id=batch_id,
            evaluation_goal=(
                source_checkpoint.evaluation_goal
                if source_checkpoint is not None and source_checkpoint.evaluation_goal
                else f"Evaluate whether {batch_id} achieved its intended goal."
            ),
            evaluation_focus=evaluation_focus,
            can_introduce_new_tasks=(
                source_checkpoint.can_introduce_new_tasks
                if source_checkpoint is not None
                else True
            ),
            can_resequence_remaining_work=(
                source_checkpoint.can_resequence_remaining_work
                if source_checkpoint is not None
                else True
            ),
        )
        normalized_checkpoints.append(normalized_checkpoint)

    return ExecutionPlan(
        plan_version=plan_version,
        supersedes_plan_version=supersedes_plan_version,
        planning_scope=raw_plan.planning_scope,
        global_goal=raw_plan.global_goal,
        execution_batches=normalized_batches,
        checkpoints=normalized_checkpoints,
        ready_task_ids=list(raw_plan.ready_task_ids),
        blocked_task_ids=list(raw_plan.blocked_task_ids),
        inferred_dependencies=list(raw_plan.inferred_dependencies),
        sequencing_rationale=raw_plan.sequencing_rationale,
        uncertainties=list(raw_plan.uncertainties),
    )


def build_execution_plan_input(
    db: Session,
    project_id: int,
) -> ExecutionPlanGenerationInput:
    project = db.get(Project, project_id)
    if not project:
        raise ExecutionPlanServiceError(f"Project {project_id} not found")

    candidate_tasks = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
            Task.status == TASK_STATUS_PENDING,
            Task.is_blocked.is_(False),
        )
        .order_by(Task.id.asc())
        .all()
    )

    if not candidate_tasks:
        raise ExecutionPlanServiceError(
            f"Project {project_id} has no active pending atomic tasks to sequence"
        )

    candidate_atomic_tasks = [
        _build_candidate_atomic_task(task, task.parent_task) for task in candidate_tasks
    ]

    execution_state = _build_execution_state_summary(db=db, project_id=project_id)

    instructions = ExecutionSequencingInstructions(
        goal=(
            "Return a safe, reasoned execution plan for the active pending atomic tasks, "
            "including batches, inferred dependencies, and mandatory checkpoints on every batch."
        ),
        requirements=[
            "Infer dependencies conservatively when needed",
            "Prioritize tasks that unlock other tasks",
            "Mark blocked tasks explicitly",
            "Every batch must end with a checkpoint",
            "The final batch must end with a closure checkpoint",
            "Assume the execution plan is revisable after each checkpoint",
        ],
        checkpoint_policy=(
            "Create a checkpoint after every batch. Use checkpoint reasons and evaluation goals "
            "that match the semantic risk and integration scope of the batch. "
            "The final checkpoint must explicitly support stage closure."
        ),
    )

    return ExecutionPlanGenerationInput(
        project_context=_build_project_context(project),
        candidate_atomic_tasks=candidate_atomic_tasks,
        execution_state=execution_state,
        instructions=instructions,
    )


def _project_has_persisted_execution_plan(db: Session, project_id: int) -> bool:
    existing = (
        db.query(Artifact.id)
        .filter(
            Artifact.project_id == project_id,
            Artifact.artifact_type == "execution_plan",
        )
        .first()
    )
    return existing is not None


def generate_execution_plan(
    db: Session,
    project_id: int,
) -> ExecutionPlan:
    project = db.get(Project, project_id)
    if not project:
        raise ExecutionPlanServiceError(f"Project {project_id} not found")

    sequencing_input = build_execution_plan_input(db=db, project_id=project_id)
    raw_plan = call_execution_sequencer_model(sequencing_input)

    has_persisted_plan = _project_has_persisted_execution_plan(db=db, project_id=project_id)
    plan_version = _resolve_generated_plan_version(
        project=project,
        has_persisted_plan=has_persisted_plan,
    )
    supersedes_plan_version = project.plan_version if has_persisted_plan else None

    return _normalize_execution_plan(
        raw_plan=raw_plan,
        plan_version=plan_version,
        supersedes_plan_version=supersedes_plan_version,
    )


def persist_execution_plan(
    db: Session,
    project_id: int,
    plan: ExecutionPlan,
    created_by: str = "execution_sequencer_agent",
) -> Artifact:
    project = db.get(Project, project_id)
    if not project:
        raise ExecutionPlanServiceError(f"Project {project_id} not found")

    has_persisted_plan = _project_has_persisted_execution_plan(db=db, project_id=project_id)
    expected_plan_version = _resolve_generated_plan_version(
        project=project,
        has_persisted_plan=has_persisted_plan,
    )
    expected_supersedes = project.plan_version if has_persisted_plan else None

    if plan.plan_version != expected_plan_version:
        raise ExecutionPlanServiceError(
            f"Execution plan version mismatch for project {project_id}: "
            f"expected plan_version={expected_plan_version}, got plan_version={plan.plan_version}."
        )

    if plan.supersedes_plan_version != expected_supersedes:
        raise ExecutionPlanServiceError(
            f"Execution plan supersedes mismatch for project {project_id}: "
            f"expected supersedes_plan_version={expected_supersedes}, "
            f"got supersedes_plan_version={plan.supersedes_plan_version}."
        )

    content = _serialize_execution_plan(plan)
    artifact = create_artifact(
        db=db,
        project_id=project_id,
        task_id=None,
        artifact_type="execution_plan",
        content=content,
        created_by=created_by,
    )

    project.plan_version = plan.plan_version
    db.add(project)
    db.commit()
    db.refresh(project)
    db.refresh(artifact)

    return artifact