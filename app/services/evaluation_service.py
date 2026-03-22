import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.task import Task
from app.services.artifacts import create_artifact
from app.services.evaluation_client import call_evaluation_model
from app.schemas.evaluation import (
    ArtifactDelta,
    ContentEvidence,
    EvaluationDecision,
    EvaluationInput,
    EvaluatedTaskEvidence,
    ExecutedTaskDelta,
    NextBatchContext,
    RecoveryContext,
    RemainingPlanSummary,
    TaskArtifactEvidence,
    TaskExecutionEvidence,
)
from app.schemas.execution_plan import ExecutionBatch, ExecutionPlan


class EvaluationServiceError(Exception):
    """Base exception for evaluation service errors."""


def _serialize_evaluation_decision(decision: EvaluationDecision) -> str:
    return json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _artifact_to_delta(artifact: Artifact) -> ArtifactDelta:
    return ArtifactDelta(
        artifact_id=artifact.id,
        artifact_type=artifact.artifact_type,
        task_id=artifact.task_id,
        summary=(
            artifact.content[:500] + "..."
            if artifact.content and len(artifact.content) > 500
            else (artifact.content or "")
        ),
    )


def _artifact_to_task_evidence(artifact: Artifact, excerpt_limit: int = 4000) -> TaskArtifactEvidence:
    content = artifact.content or ""
    full_content_included = len(content) <= excerpt_limit
    excerpt = content if full_content_included else content[:excerpt_limit] + "..."
    return TaskArtifactEvidence(
        artifact_id=artifact.id,
        artifact_type=artifact.artifact_type,
        content_excerpt=excerpt,
        full_content_included=full_content_included,
    )


def _batch_to_next_batch_context(batch: ExecutionBatch) -> NextBatchContext:
    return NextBatchContext(
        batch_id=batch.batch_id,
        name=batch.name,
        goal=batch.goal,
        task_ids=batch.task_ids,
        entry_conditions=batch.entry_conditions,
        expected_outputs=batch.expected_outputs,
        risk_level=batch.risk_level,
    )


def _build_current_project_state_summary(
    executed_tasks: list[ExecutedTaskDelta],
    artifacts: list[ArtifactDelta],
    next_batch: NextBatchContext | None,
    recovery_context: RecoveryContext,
    content_evidence: ContentEvidence,
) -> str:
    executed_titles = ", ".join(task.title for task in executed_tasks) or "none"
    artifact_types = ", ".join(artifact.artifact_type for artifact in artifacts) or "none"
    next_batch_name = next_batch.name if next_batch else "none"
    recovery_decision_count = len(recovery_context.recovery_decisions)
    open_issue_count = len(recovery_context.open_issues)
    recovery_created_task_count = len(recovery_context.recovery_created_tasks)
    evidence_count = len(content_evidence.evaluated_tasks)

    return (
        "Checkpoint evaluation context summary:\n"
        f"- Executed tasks since last checkpoint: {executed_titles}\n"
        f"- Artifacts produced since last checkpoint: {artifact_types}\n"
        f"- Next planned batch: {next_batch_name}\n"
        f"- Recovery decisions in this checkpoint window: {recovery_decision_count}\n"
        f"- Open issues after recovery: {open_issue_count}\n"
        f"- New tasks created by recovery: {recovery_created_task_count}\n"
        f"- Tasks with content evidence available: {evidence_count}\n"
        "- The evaluator must assess whether the completed work plus recovery outcomes "
        "are sufficient to safely continue with the next planned segment."
    )


def _build_content_evidence(
    db: Session,
    project_id: int,
    tasks: list[Task],
) -> ContentEvidence:
    evaluated_tasks: list[EvaluatedTaskEvidence] = []

    for task in tasks:
        last_run = (
            db.query(ExecutionRun)
            .filter(ExecutionRun.task_id == task.id)
            .order_by(ExecutionRun.id.desc())
            .first()
        )

        task_artifacts = (
            db.query(Artifact)
            .filter(
                Artifact.project_id == project_id,
                Artifact.task_id == task.id,
            )
            .order_by(Artifact.id.asc())
            .all()
        )

        execution_evidence = TaskExecutionEvidence(
            run_id=last_run.id if last_run else None,
            run_status=last_run.status if last_run else None,
            work_summary=last_run.work_summary if last_run else None,
            work_details=last_run.work_details if last_run else None,
            completed_scope=last_run.completed_scope if last_run else None,
            remaining_scope=last_run.remaining_scope if last_run else None,
            blockers_found=last_run.blockers_found if last_run else None,
            validation_notes=last_run.validation_notes if last_run else None,
            error_message=last_run.error_message if last_run else None,
        )

        artifact_evidence = [
            _artifact_to_task_evidence(artifact)
            for artifact in task_artifacts
        ]

        evaluated_tasks.append(
            EvaluatedTaskEvidence(
                task_id=task.id,
                title=task.title,
                description=task.description,
                objective=task.objective,
                acceptance_criteria=task.acceptance_criteria,
                tests_required=task.tests_required,
                execution_evidence=execution_evidence,
                artifact_evidence=artifact_evidence,
            )
        )

    return ContentEvidence(evaluated_tasks=evaluated_tasks)


def build_evaluation_input(
    db: Session,
    project_id: int,
    plan: ExecutionPlan,
    checkpoint_id: str,
    executed_task_ids_since_last_checkpoint: list[int],
    artifact_ids_since_last_checkpoint: list[int],
    recovery_context: RecoveryContext | None = None,
) -> EvaluationInput:
    project = db.get(Project, project_id)
    if not project:
        raise EvaluationServiceError(f"Project {project_id} not found")

    checkpoint = next((cp for cp in plan.checkpoints if cp.checkpoint_id == checkpoint_id), None)
    if not checkpoint:
        raise EvaluationServiceError(
            f"Checkpoint '{checkpoint_id}' not found in execution plan version {plan.plan_version}"
        )

    checkpoint_batch_index = next(
        (
            index
            for index, batch in enumerate(plan.execution_batches)
            if batch.batch_id == checkpoint.after_batch_id
        ),
        None,
    )
    if checkpoint_batch_index is None:
        raise EvaluationServiceError(
            f"Checkpoint '{checkpoint_id}' references unknown batch '{checkpoint.after_batch_id}'"
        )

    next_batch = None
    if checkpoint_batch_index + 1 < len(plan.execution_batches):
        next_batch = _batch_to_next_batch_context(plan.execution_batches[checkpoint_batch_index + 1])

    remaining_batches = plan.execution_batches[checkpoint_batch_index + 1 :]
    resolved_recovery_context = recovery_context or RecoveryContext()

    executed_tasks: list[ExecutedTaskDelta] = []
    executed_task_objects: list[Task] = []

    if executed_task_ids_since_last_checkpoint:
        tasks = (
            db.query(Task)
            .filter(Task.project_id == project_id, Task.id.in_(executed_task_ids_since_last_checkpoint))
            .order_by(Task.id.asc())
            .all()
        )
        executed_task_objects = tasks

        for task in tasks:
            last_run = (
                db.query(ExecutionRun)
                .filter(ExecutionRun.task_id == task.id)
                .order_by(ExecutionRun.id.desc())
                .first()
            )
            executed_tasks.append(
                ExecutedTaskDelta(
                    task_id=task.id,
                    title=task.title,
                    task_status=task.status,
                    last_run_status=last_run.status if last_run else None,
                    work_summary=last_run.work_summary if last_run else None,
                    completed_scope=last_run.completed_scope if last_run else None,
                    remaining_scope=last_run.remaining_scope if last_run else None,
                    blockers_found=last_run.blockers_found if last_run else None,
                    validation_notes=last_run.validation_notes if last_run else None,
                )
            )

    artifact_deltas: list[ArtifactDelta] = []
    if artifact_ids_since_last_checkpoint:
        artifacts = (
            db.query(Artifact)
            .filter(Artifact.project_id == project_id, Artifact.id.in_(artifact_ids_since_last_checkpoint))
            .order_by(Artifact.id.asc())
            .all()
        )
        artifact_deltas = [_artifact_to_delta(artifact) for artifact in artifacts]

    remaining_plan = RemainingPlanSummary(
        pending_batch_ids=[batch.batch_id for batch in remaining_batches],
        pending_task_ids=[task_id for batch in remaining_batches for task_id in batch.task_ids],
        blocked_task_ids=plan.blocked_task_ids,
        sequencing_rationale=plan.sequencing_rationale,
    )

    content_evidence = _build_content_evidence(
        db=db,
        project_id=project_id,
        tasks=executed_task_objects,
    )

    return EvaluationInput(
        project_id=project.id,
        project_name=project.name,
        project_goal=project.description or project.name,
        current_execution_objective=plan.global_goal,
        plan_version=plan.plan_version,
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_name=checkpoint.name,
        checkpoint_reason=checkpoint.reason,
        checkpoint_evaluation_goal=checkpoint.evaluation_goal,
        executed_tasks_since_last_checkpoint=executed_tasks,
        artifacts_since_last_checkpoint=artifact_deltas,
        current_project_state_summary=_build_current_project_state_summary(
            executed_tasks=executed_tasks,
            artifacts=artifact_deltas,
            next_batch=next_batch,
            recovery_context=resolved_recovery_context,
            content_evidence=content_evidence,
        ),
        next_batch=next_batch,
        remaining_plan=remaining_plan,
        recovery_context=resolved_recovery_context,
        content_evidence=content_evidence,
    )


def evaluate_checkpoint(
    db: Session,
    project_id: int,
    plan: ExecutionPlan,
    checkpoint_id: str,
    executed_task_ids_since_last_checkpoint: list[int],
    artifact_ids_since_last_checkpoint: list[int],
    recovery_context: RecoveryContext | None = None,
) -> EvaluationDecision:
    evaluation_input = build_evaluation_input(
        db=db,
        project_id=project_id,
        plan=plan,
        checkpoint_id=checkpoint_id,
        executed_task_ids_since_last_checkpoint=executed_task_ids_since_last_checkpoint,
        artifact_ids_since_last_checkpoint=artifact_ids_since_last_checkpoint,
        recovery_context=recovery_context,
    )
    return call_evaluation_model(evaluation_input)


def persist_evaluation_decision(
    db: Session,
    project_id: int,
    decision: EvaluationDecision,
    created_by: str = "evaluation_agent",
) -> Artifact:
    project = db.get(Project, project_id)
    if not project:
        raise EvaluationServiceError(f"Project {project_id} not found")

    content = _serialize_evaluation_decision(decision)
    return create_artifact(
        db=db,
        project_id=project_id,
        task_id=None,
        artifact_type="evaluation_decision",
        content=content,
        created_by=created_by,
    )