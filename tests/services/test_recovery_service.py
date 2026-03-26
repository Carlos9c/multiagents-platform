import pytest
from pydantic import ValidationError

from app.models.task import (
    PENDING_ENGINE_ROUTING_EXECUTOR,
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
)
from app.schemas.recovery import RecoveryDecision
from app.services.recovery_service import (
    RecoveryServiceError,
    build_recovery_context_entry,
    materialize_recovery_decision,
)


def test_recovery_decision_schema_rejects_unknown_retry_field():
    with pytest.raises(ValidationError):
        RecoveryDecision.model_validate(
            {
                "source_task_id": 1,
                "source_run_id": 10,
                "action": "reatomize",
                "confidence": "high",
                "reason": "The task must be decomposed into smaller atomic units.",
                "covered_gap_summary": "The remaining gap is covered by the replacement tasks.",
                "still_blocks_progress": True,
                "created_tasks": [
                    {
                        "title": "Create replacement task",
                        "description": "A concrete replacement task.",
                    }
                ],
                "retry_same_task": True,
            }
        )


def test_reatomize_creates_new_atomic_tasks_with_pending_executor_and_keeps_source_failed(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_recovery_decision,
):
    project = make_project()

    parent = make_task(
        project_id=project.id,
        title="Parent high-level task",
        description="Parent task for recovered atomic work.",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
    )

    source_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Failed atomic task",
        description="This task failed and needs reatomization.",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_FAILED,
        sequence_order=1,
    )
    run = make_execution_run(
        task_id=source_task.id,
        status="failed",
        failure_type="execution_error",
        failure_code="tool_failed",
    )

    decision = make_recovery_decision(
        source_task_id=source_task.id,
        source_run_id=run.id,
        action="reatomize",
        created_tasks=[
            {
                "title": "Implement part A",
                "description": "Implement the first part of the failed task.",
                "objective": "Complete part A.",
                "implementation_notes": "Touch only the first area.",
                "acceptance_criteria": "Part A is implemented.",
            },
            {
                "title": "Implement part B",
                "description": "Implement the second part of the failed task.",
                "objective": "Complete part B.",
                "implementation_notes": "Touch only the second area.",
                "acceptance_criteria": "Part B is implemented.",
            },
        ],
        still_blocks_progress=True,
    )

    created_tasks = materialize_recovery_decision(
        db=db_session,
        project_id=project.id,
        decision=decision,
    )

    db_session.refresh(source_task)

    assert source_task.status == TASK_STATUS_FAILED
    assert len(created_tasks) == 2

    for created in created_tasks:
        assert created.project_id == project.id
        assert created.parent_task_id == parent.id
        assert created.planning_level == PLANNING_LEVEL_ATOMIC
        assert created.executor_type == PENDING_ENGINE_ROUTING_EXECUTOR
        assert created.status != TASK_STATUS_FAILED
        assert created.status != TASK_STATUS_PARTIAL

    assert created_tasks[0].sequence_order is not None
    assert created_tasks[1].sequence_order is not None
    assert created_tasks[0].sequence_order < created_tasks[1].sequence_order


def test_manual_review_keeps_source_partial_and_creates_no_tasks(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_recovery_decision,
):
    project = make_project()

    parent = make_task(
        project_id=project.id,
        title="Parent high-level task",
        description="Parent task for recovered atomic work.",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
    )

    source_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Partial atomic task",
        description="This task ended partial and requires review.",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_PARTIAL,
        sequence_order=1,
    )
    run = make_execution_run(
        task_id=source_task.id,
        status="partial",
        failure_type="validation_failed",
        failure_code="scope_incomplete",
    )

    decision = make_recovery_decision(
        source_task_id=source_task.id,
        source_run_id=run.id,
        action="manual_review",
        requires_manual_review=True,
        created_tasks=[],
        still_blocks_progress=True,
        reason="Automated recovery is not trustworthy enough.",
        covered_gap_summary="The remaining gap requires human judgment.",
    )

    created_tasks = materialize_recovery_decision(
        db=db_session,
        project_id=project.id,
        decision=decision,
    )

    db_session.refresh(source_task)

    assert source_task.status == TASK_STATUS_PARTIAL
    assert created_tasks == []


def test_materialize_recovery_fails_if_source_atomic_has_no_parent_task_id(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_recovery_decision,
):
    project = make_project()

    source_task = make_task(
        project_id=project.id,
        parent_task_id=None,
        title="Orphan failed atomic task",
        description="Atomic task without structural parent.",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_FAILED,
        sequence_order=1,
    )
    run = make_execution_run(
        task_id=source_task.id,
        status="failed",
        failure_type="execution_error",
        failure_code="tool_failed",
    )

    decision = make_recovery_decision(
        source_task_id=source_task.id,
        source_run_id=run.id,
        action="insert_followup",
        created_tasks=[
            {
                "title": "Create follow-up task",
                "description": "A follow-up task that should not be materialized without a valid parent.",
                "objective": "Cover the remaining gap.",
                "implementation_notes": "Use the recovery output.",
                "acceptance_criteria": "The gap is covered.",
            }
        ],
        still_blocks_progress=False,
    )

    with pytest.raises(RecoveryServiceError, match="has no parent_task_id"):
        materialize_recovery_decision(
            db=db_session,
            project_id=project.id,
            decision=decision,
        )


def test_build_recovery_context_entry_keeps_created_task_records_and_open_issue(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_recovery_decision,
):
    project = make_project()

    parent = make_task(
        project_id=project.id,
        title="Parent high-level task",
        description="Parent task for recovered atomic work.",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
    )

    source_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Failed atomic task",
        description="Task used to test recovery context entry.",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_FAILED,
        sequence_order=1,
    )
    run = make_execution_run(
        task_id=source_task.id,
        status="failed",
        failure_type="execution_error",
        failure_code="tool_failed",
    )

    decision = make_recovery_decision(
        source_task_id=source_task.id,
        source_run_id=run.id,
        action="insert_followup",
        created_tasks=[
            {
                "title": "Add follow-up task",
                "description": "A follow-up task that complements the failed source task.",
                "objective": "Cover the remaining gap.",
                "implementation_notes": "Use the generated context.",
                "acceptance_criteria": "The remaining gap is covered.",
            }
        ],
        still_blocks_progress=True,
        evaluation_guidance="Interpret the recovery as a blocking local gap.",
    )

    created_tasks = materialize_recovery_decision(
        db=db_session,
        project_id=project.id,
        decision=decision,
    )

    context = build_recovery_context_entry(
        decision=decision,
        created_tasks=created_tasks,
    )

    assert len(context.recovery_decisions) == 1
    assert context.recovery_decisions[0].action == "insert_followup"
    assert context.recovery_decisions[0].source_task_id == source_task.id
    assert len(context.recovery_decisions[0].created_task_ids) == 1

    assert len(context.recovery_created_tasks) == 1
    assert context.recovery_created_tasks[0].created_task_id == created_tasks[0].id
    assert context.recovery_created_tasks[0].source_task_id == source_task.id

    assert len(context.open_issues) == 1
    assert context.open_issues[0].issue_type == "progress_blocked"
    assert context.open_issues[0].source_task_id == source_task.id