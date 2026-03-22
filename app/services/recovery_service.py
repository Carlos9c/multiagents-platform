import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_PENDING,
    Task,
)
from app.services.artifacts import create_artifact
from app.services.recovery_client import call_recovery_model
from app.schemas.evaluation import (
    RecoveryContext,
    RecoveryCreatedTaskSummary,
    RecoveryDecisionSummary,
    RecoveryOpenIssue,
)
from app.schemas.recovery import (
    RecoveryArtifactSummary,
    RecoveryDecision,
    RecoveryExecutionRunContext,
    RecoveryInput,
    RecoveryProjectContext,
    RecoveryProposedTask,
    RecoveryRecentRunSummary,
    RecoveryTaskContext,
)


class RecoveryServiceError(Exception):
    """Base exception for recovery service errors."""


def _serialize_recovery_decision(decision: RecoveryDecision) -> str:
    return json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _build_project_context(project: Project) -> RecoveryProjectContext:
    return RecoveryProjectContext(
        project_id=project.id,
        project_name=project.name,
        project_goal=project.description or project.name,
        current_execution_objective=(
            "Resolve local execution incidents safely before global evaluation decides whether execution may continue."
        ),
    )


def _build_task_context(task: Task, parent_task: Task | None) -> RecoveryTaskContext:
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

    return RecoveryTaskContext(
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


def _build_execution_run_context(run: ExecutionRun) -> RecoveryExecutionRunContext:
    return RecoveryExecutionRunContext(
        run_id=run.id,
        attempt_number=run.attempt_number,
        status=run.status,
        input_snapshot=run.input_snapshot,
        output_snapshot=run.output_snapshot,
        error_message=run.error_message,
        failure_type=run.failure_type,
        failure_code=run.failure_code,
        recovery_action=run.recovery_action,
        work_summary=run.work_summary,
        work_details=run.work_details,
        artifacts_created=run.artifacts_created,
        completed_scope=run.completed_scope,
        remaining_scope=run.remaining_scope,
        blockers_found=run.blockers_found,
        validation_notes=run.validation_notes,
    )


def _build_recent_runs_for_task(
    db: Session,
    task_id: int,
    exclude_run_id: int,
    limit: int = 5,
) -> list[RecoveryRecentRunSummary]:
    runs = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id, ExecutionRun.id != exclude_run_id)
        .order_by(ExecutionRun.id.desc())
        .limit(limit)
        .all()
    )

    return [
        RecoveryRecentRunSummary(
            run_id=run.id,
            attempt_number=run.attempt_number,
            status=run.status,
            failure_type=run.failure_type,
            failure_code=run.failure_code,
            work_summary=run.work_summary,
            completed_scope=run.completed_scope,
            remaining_scope=run.remaining_scope,
            blockers_found=run.blockers_found,
        )
        for run in runs
    ]


def _build_relevant_artifacts(
    db: Session,
    project_id: int,
    task_id: int,
    limit: int = 10,
) -> list[RecoveryArtifactSummary]:
    artifacts = (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            ((Artifact.task_id == task_id) | (Artifact.task_id.is_(None))),
        )
        .order_by(Artifact.id.desc())
        .limit(limit)
        .all()
    )

    summaries: list[RecoveryArtifactSummary] = []
    for artifact in artifacts:
        content = artifact.content or ""
        summaries.append(
            RecoveryArtifactSummary(
                artifact_id=artifact.id,
                artifact_type=artifact.artifact_type,
                task_id=artifact.task_id,
                summary=(content[:500] + "...") if len(content) > 500 else content,
            )
        )
    return summaries


def _build_recovery_input(
    db: Session,
    run_id: int,
    next_batch_summary: str | None = None,
    remaining_plan_summary: str | None = None,
) -> RecoveryInput:
    run = db.get(ExecutionRun, run_id)
    if not run:
        raise RecoveryServiceError(f"ExecutionRun {run_id} not found")

    task = db.get(Task, run.task_id)
    if not task:
        raise RecoveryServiceError(f"Task {run.task_id} not found")

    project = db.get(Project, task.project_id)
    if not project:
        raise RecoveryServiceError(f"Project {task.project_id} not found")

    return RecoveryInput(
        project_context=_build_project_context(project),
        task=_build_task_context(task, task.parent_task),
        execution_run=_build_execution_run_context(run),
        recent_runs_for_task=_build_recent_runs_for_task(
            db=db,
            task_id=task.id,
            exclude_run_id=run.id,
        ),
        relevant_artifacts=_build_relevant_artifacts(
            db=db,
            project_id=project.id,
            task_id=task.id,
        ),
        next_batch_summary=next_batch_summary,
        remaining_plan_summary=remaining_plan_summary,
        coordination_rules_summary=(
            "Recovery acts before evaluation and should resolve the local task incident "
            "without duplicating global planning. Its output will be consumed by the evaluator."
        ),
    )


def generate_recovery_decision(
    db: Session,
    run_id: int,
    next_batch_summary: str | None = None,
    remaining_plan_summary: str | None = None,
) -> RecoveryDecision:
    recovery_input = _build_recovery_input(
        db=db,
        run_id=run_id,
        next_batch_summary=next_batch_summary,
        remaining_plan_summary=remaining_plan_summary,
    )
    return call_recovery_model(recovery_input)


def persist_recovery_decision(
    db: Session,
    project_id: int,
    decision: RecoveryDecision,
    created_by: str = "recovery_agent",
) -> Artifact:
    project = db.get(Project, project_id)
    if not project:
        raise RecoveryServiceError(f"Project {project_id} not found")

    content = _serialize_recovery_decision(decision)
    return create_artifact(
        db=db,
        project_id=project_id,
        task_id=decision.source_task_id,
        artifact_type="recovery_decision",
        content=content,
        created_by=created_by,
    )


def _create_task_from_recovery_proposal(
    db: Session,
    project_id: int,
    source_task: Task,
    proposed_task: RecoveryProposedTask,
) -> Task:
    new_task = Task(
        project_id=project_id,
        parent_task_id=source_task.parent_task_id,
        title=proposed_task.title,
        description=proposed_task.description,
        objective=proposed_task.objective,
        task_type=proposed_task.task_type,
        priority=proposed_task.priority,
        planning_level=PLANNING_LEVEL_ATOMIC,
        executor_type=source_task.executor_type,
        status=TASK_STATUS_PENDING,
        technical_constraints=proposed_task.technical_constraints,
        out_of_scope=proposed_task.out_of_scope,
        summary=f"Created by recovery_agent as follow-up to task {source_task.id}.",
    )
    db.add(new_task)
    db.flush()
    db.refresh(new_task)
    return new_task


def materialize_recovery_decision(
    db: Session,
    project_id: int,
    decision: RecoveryDecision,
) -> list[Task]:
    project = db.get(Project, project_id)
    if not project:
        raise RecoveryServiceError(f"Project {project_id} not found")

    source_task = db.get(Task, decision.source_task_id)
    if not source_task:
        raise RecoveryServiceError(f"Source task {decision.source_task_id} not found")

    created_tasks: list[Task] = []

    if decision.proposed_tasks:
        for proposed_task in decision.proposed_tasks:
            created_tasks.append(
                _create_task_from_recovery_proposal(
                    db=db,
                    project_id=project_id,
                    source_task=source_task,
                    proposed_task=proposed_task,
                )
            )

    if decision.should_mark_source_task_obsolete:
        source_task.is_blocked = True
        source_task.blocking_reason = (
            f"Superseded by recovery decision from run {decision.source_run_id}."
        )
        db.add(source_task)

    db.commit()

    for task in created_tasks:
        db.refresh(task)

    return created_tasks


def build_recovery_context_entry(
    decision: RecoveryDecision,
    created_tasks: list[Task] | None = None,
) -> RecoveryContext:
    resolved_created_tasks = created_tasks or []

    recovery_decision_summary = RecoveryDecisionSummary(
        source_task_id=decision.source_task_id,
        source_run_id=decision.source_run_id,
        run_status="recovered_decision",
        decision_type=decision.decision_type,
        reason=decision.reason,
        replacement_task_ids=[task.id for task in resolved_created_tasks],
        still_blocks_progress=decision.still_blocks_progress,
        covered_gap_summary=decision.covered_gap_summary,
    )

    open_issues: list[RecoveryOpenIssue] = []
    if decision.still_blocks_progress:
        open_issues.append(
            RecoveryOpenIssue(
                task_id=decision.source_task_id,
                issue_type=decision.decision_type,
                remaining_scope=decision.covered_gap_summary,
                blockers_found=decision.execution_guidance,
                why_it_still_matters=decision.evaluation_guidance,
            )
        )

    recovery_created_tasks = [
        RecoveryCreatedTaskSummary(
            task_id=task.id,
            title=task.title,
            objective=task.objective,
            origin="recovery_agent",
            source_task_id=decision.source_task_id,
        )
        for task in resolved_created_tasks
    ]

    return RecoveryContext(
        recovery_decisions=[recovery_decision_summary],
        open_issues=open_issues,
        recovery_created_tasks=recovery_created_tasks,
    )


def merge_recovery_contexts(
    contexts: list[RecoveryContext],
) -> RecoveryContext:
    merged = RecoveryContext()

    for context in contexts:
        merged.recovery_decisions.extend(context.recovery_decisions)
        merged.open_issues.extend(context.open_issues)
        merged.recovery_created_tasks.extend(context.recovery_created_tasks)

    return merged