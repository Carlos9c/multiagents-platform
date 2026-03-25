import json
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.schemas.recovery import (
    RecoveryContext,
    RecoveryCreatedTaskRecord,
    RecoveryDecision,
    RecoveryDecisionSummary,
    RecoveryOpenIssue,
)
from app.services.artifacts import create_artifact


RECOVERY_DECISION_ARTIFACT_TYPE = "recovery_decision"


class RecoveryServiceError(Exception):
    """Base exception for recovery service errors."""


def _serialize_recovery_decision(decision: RecoveryDecision) -> str:
    return json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _get_run_or_raise(db: Session, run_id: int) -> ExecutionRun:
    run = db.get(ExecutionRun, run_id)
    if not run:
        raise RecoveryServiceError(f"ExecutionRun {run_id} not found")
    return run


def _get_task_or_raise(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise RecoveryServiceError(f"Task {task_id} not found")
    return task


def _get_source_entities_or_raise(
    db: Session,
    *,
    decision: RecoveryDecision,
) -> tuple[ExecutionRun, Task]:
    run = _get_run_or_raise(db, decision.source_run_id)
    task = _get_task_or_raise(db, decision.source_task_id)

    if run.task_id != task.id:
        raise RecoveryServiceError(
            f"Recovery decision is inconsistent: run {run.id} belongs to task {run.task_id}, "
            f"but decision.source_task_id={task.id}."
        )

    return run, task


def _ensure_source_task_is_recoverable(source_task: Task) -> None:
    if source_task.planning_level != PLANNING_LEVEL_ATOMIC:
        raise RecoveryServiceError(
            f"Recovery can only materialize decisions for atomic tasks. "
            f"Task {source_task.id} has planning_level='{source_task.planning_level}'."
        )

    if source_task.status not in TERMINAL_TASK_STATUSES:
        raise RecoveryServiceError(
            f"Recovery requires the source task to be terminal before materialization. "
            f"Task {source_task.id} has status='{source_task.status}'."
        )

    if source_task.status not in {TASK_STATUS_FAILED, TASK_STATUS_PARTIAL}:
        raise RecoveryServiceError(
            f"Recovery only applies to failed or partial atomic tasks. "
            f"Task {source_task.id} has status='{source_task.status}'."
        )


def _infer_parent_task_id_for_created_tasks(source_task: Task) -> int:
    return source_task.parent_task_id or source_task.id


def _build_created_task_from_recovery(
    *,
    project_id: int,
    parent_task_id: int,
    task_create,
    sequence_order: int,
) -> Task:
    return Task(
        project_id=project_id,
        parent_task_id=parent_task_id,
        title=task_create.title,
        description=task_create.description,
        summary=task_create.description,
        objective=task_create.objective or task_create.description,
        proposed_solution=task_create.implementation_notes,
        implementation_notes=task_create.implementation_notes,
        implementation_steps=None,
        acceptance_criteria=task_create.acceptance_criteria,
        tests_required=None,
        technical_constraints=task_create.technical_constraints,
        out_of_scope=task_create.out_of_scope,
        priority=task_create.priority,
        task_type=task_create.task_type,
        planning_level=PLANNING_LEVEL_ATOMIC,
        executor_type=task_create.executor_type,
        sequence_order=sequence_order,
        status=TASK_STATUS_PENDING,
        is_blocked=False,
        blocking_reason=None,
    )


def _build_source_task_summary(
    *,
    source_task: Task,
    source_run: ExecutionRun,
) -> str:
    payload = {
        "source_task": {
            "task_id": source_task.id,
            "title": source_task.title,
            "description": source_task.description,
            "summary": source_task.summary,
            "objective": source_task.objective,
            "task_type": source_task.task_type,
            "planning_level": source_task.planning_level,
            "status": source_task.status,
            "acceptance_criteria": source_task.acceptance_criteria,
            "technical_constraints": source_task.technical_constraints,
            "out_of_scope": source_task.out_of_scope,
            "parent_task_id": source_task.parent_task_id,
        },
        "source_run": {
            "run_id": source_run.id,
            "status": source_run.status,
            "failure_type": source_run.failure_type,
            "failure_code": source_run.failure_code,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def persist_recovery_decision(
    db: Session,
    *,
    project_id: int,
    decision: RecoveryDecision,
    created_by: str = "recovery_agent",
) -> Artifact:
    _get_source_entities_or_raise(db, decision=decision)

    artifact = create_artifact(
        db=db,
        project_id=project_id,
        task_id=decision.source_task_id,
        artifact_type=RECOVERY_DECISION_ARTIFACT_TYPE,
        content=_serialize_recovery_decision(decision),
        created_by=created_by,
    )
    return artifact


def generate_recovery_decision(
    db: Session,
    *,
    run_id: int,
    execution_context_summary: str,
    validation_context_summary: str,
    next_batch_summary: str | None = None,
    remaining_plan_summary: str | None = None,
):
    run = _get_run_or_raise(db, run_id)
    source_task = _get_task_or_raise(db, run.task_id)

    from app.services.recovery_client import call_recovery_model

    source_task_summary = _build_source_task_summary(
        source_task=source_task,
        source_run=run,
    )

    decision = call_recovery_model(
        source_task_summary=source_task_summary,
        execution_context_summary=execution_context_summary,
        validation_context_summary=validation_context_summary,
        next_batch_summary=next_batch_summary,
        remaining_plan_summary=remaining_plan_summary,
    )

    if decision.source_run_id != run.id:
        raise RecoveryServiceError(
            f"Recovery decision source_run_id mismatch: expected {run.id}, got {decision.source_run_id}."
        )

    if decision.source_task_id != source_task.id:
        raise RecoveryServiceError(
            f"Recovery decision source_task_id mismatch: expected {source_task.id}, got {decision.source_task_id}."
        )

    return decision


def materialize_recovery_decision(
    db: Session,
    *,
    project_id: int,
    decision: RecoveryDecision,
) -> list[Task]:
    _, source_task = _get_source_entities_or_raise(db, decision=decision)

    if source_task.project_id != project_id:
        raise RecoveryServiceError(
            f"Source task {source_task.id} does not belong to project {project_id}."
        )

    _ensure_source_task_is_recoverable(source_task)
    original_status = source_task.status

    if decision.action == "manual_review":
        if decision.created_tasks:
            raise RecoveryServiceError(
                "Recovery action 'manual_review' must not contain created tasks."
            )

        db.refresh(source_task)
        if source_task.status != original_status:
            raise RecoveryServiceError(
                f"Recovery integrity error: source task {source_task.id} changed status from "
                f"'{original_status}' to '{source_task.status}' during manual_review materialization."
            )

        return []

    normalized_action = decision.action

    # Defensive normalization for legacy artifacts or stale model outputs.
    # The active workflow no longer supports retry because the source atomic
    # task must remain terminal. Degrade it gracefully instead of crashing.
    if normalized_action == "retry":
        if decision.created_tasks:
            normalized_action = "insert_followup"
        else:
            normalized_action = "manual_review"

    if normalized_action not in {"reatomize", "insert_followup", "manual_review"}:
        raise RecoveryServiceError(f"Unsupported recovery action '{normalized_action}'.")

    if normalized_action == "manual_review":
        db.refresh(source_task)
        if source_task.status != original_status:
            raise RecoveryServiceError(
                f"Recovery integrity error: source task {source_task.id} changed status from "
                f"'{original_status}' to '{source_task.status}' during manual_review materialization."
            )
        return []

    if not decision.created_tasks:
        raise RecoveryServiceError(
            f"Recovery action '{normalized_action}' requires created_tasks."
        )

    if decision.action not in {"reatomize", "insert_followup"}:
        raise RecoveryServiceError(f"Unsupported recovery action '{decision.action}'.")

    if decision.action not in {"reatomize", "insert_followup"}:
        raise RecoveryServiceError(f"Unsupported recovery action '{decision.action}'.")

    if not decision.created_tasks:
        raise RecoveryServiceError(
            f"Recovery action '{decision.action}' requires created_tasks."
        )

    parent_task_id = _infer_parent_task_id_for_created_tasks(source_task)

    last_sequence_order = (
        db.query(Task.sequence_order)
        .filter(
            Task.project_id == project_id,
            Task.parent_task_id == parent_task_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
        )
        .order_by(Task.sequence_order.desc(), Task.id.desc())
        .first()
    )
    start_sequence = (last_sequence_order[0] if last_sequence_order and last_sequence_order[0] else 0) + 1

    created_tasks: list[Task] = []

    for index, task_create in enumerate(decision.created_tasks, start=0):
        task = _build_created_task_from_recovery(
            project_id=project_id,
            parent_task_id=parent_task_id,
            task_create=task_create,
            sequence_order=start_sequence + index,
        )
        db.add(task)
        created_tasks.append(task)

    db.flush()
    db.commit()

    db.refresh(source_task)

    if source_task.status != original_status:
        raise RecoveryServiceError(
            f"Recovery integrity error: source task {source_task.id} changed status from "
            f"'{original_status}' to '{source_task.status}' after creating follow-up tasks. "
            "The original atomic task must remain terminal."
        )

    return created_tasks


def build_recovery_context_entry(
    *,
    decision: RecoveryDecision,
    created_tasks: list[Task],
) -> RecoveryContext:
    created_task_records = [
        RecoveryCreatedTaskRecord(
            source_task_id=decision.source_task_id,
            source_run_id=decision.source_run_id,
            created_task_id=task.id,
            title=task.title,
            planning_level=task.planning_level,
            executor_type=task.executor_type,
        )
        for task in created_tasks
    ]

    decision_summary = RecoveryDecisionSummary(
        source_task_id=decision.source_task_id,
        source_run_id=decision.source_run_id,
        action=decision.action,
        confidence=decision.confidence,
        reason=decision.reason,
        still_blocks_progress=decision.still_blocks_progress,
        created_task_ids=[task.id for task in created_tasks],
    )

    open_issues: list[RecoveryOpenIssue] = []

    if decision.requires_manual_review:
        open_issues.append(
            RecoveryOpenIssue(
                source_task_id=decision.source_task_id,
                source_run_id=decision.source_run_id,
                issue_type="manual_review_required",
                summary=decision.covered_gap_summary,
                recommended_action=decision.reason,
            )
        )
    elif decision.still_blocks_progress:
        open_issues.append(
            RecoveryOpenIssue(
                source_task_id=decision.source_task_id,
                source_run_id=decision.source_run_id,
                issue_type=f"recovery_{decision.action}",
                summary=decision.covered_gap_summary,
                recommended_action=decision.execution_guidance or decision.reason,
            )
        )

    return RecoveryContext(
        recovery_decisions=[decision_summary],
        open_issues=open_issues,
        recovery_created_tasks=created_task_records,
    )


def merge_recovery_contexts(
    contexts: Iterable[RecoveryContext] | None,
) -> RecoveryContext:
    if not contexts:
        return RecoveryContext()

    merged_decisions: list[RecoveryDecisionSummary] = []
    merged_open_issues: list[RecoveryOpenIssue] = []
    merged_created_tasks: list[RecoveryCreatedTaskRecord] = []

    seen_decisions: set[tuple[int, int, str]] = set()
    seen_issues: set[tuple[int, int, str, str]] = set()
    seen_created_tasks: set[tuple[int, int, int]] = set()

    for context in contexts:
        for decision in context.recovery_decisions:
            key = (decision.source_task_id, decision.source_run_id, decision.action)
            if key not in seen_decisions:
                seen_decisions.add(key)
                merged_decisions.append(decision)

        for issue in context.open_issues:
            key = (issue.source_task_id, issue.source_run_id, issue.issue_type, issue.summary)
            if key not in seen_issues:
                seen_issues.add(key)
                merged_open_issues.append(issue)

        for created in context.recovery_created_tasks:
            key = (created.source_task_id, created.source_run_id, created.created_task_id)
            if key not in seen_created_tasks:
                seen_created_tasks.add(key)
                merged_created_tasks.append(created)

    return RecoveryContext(
        recovery_decisions=merged_decisions,
        open_issues=merged_open_issues,
        recovery_created_tasks=merged_created_tasks,
    )