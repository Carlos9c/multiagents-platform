# tests/services/test_recovery_service.py
import pytest
from pydantic import ValidationError

from app.schemas.recovery import RecoveryDecision
from app.models.task import (
    PENDING_ENGINE_ROUTING_EXECUTOR,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
)
from app.services.recovery_service import (
    RecoveryServiceError,
    materialize_recovery_decision,
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
        title="Parent task",
        planning_level="high_level",
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )
    source_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Failed atomic task",
        status=TASK_STATUS_FAILED,
        sequence_order=1,
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )
    run = make_execution_run(task_id=source_task.id, status="failed")

    decision = make_recovery_decision(
        source_task_id=source_task.id,
        source_run_id=run.id,
        action="reatomize",
        created_tasks=[
            {
                "title": "Create bootstrap structure",
                "description": "Create the minimal module and entrypoint structure needed to continue safely.",
                "objective": "Create the initial implementation surface.",
                "implementation_notes": "Seed conventional Python files.",
                "acceptance_criteria": "The structure exists and is ready for follow-up implementation.",
            },
            {
                "title": "Implement notes endpoints",
                "description": "Implement the create and list notes endpoints over the new structure.",
                "objective": "Restore progress through a narrower recovery task.",
                "implementation_notes": "Use in-memory storage.",
                "acceptance_criteria": "Endpoints behave according to the minimal contract.",
            },
        ],
    )

    created_tasks = materialize_recovery_decision(
        db_session,
        project_id=project.id,
        decision=decision,
    )

    db_session.refresh(source_task)

    assert source_task.status == TASK_STATUS_FAILED
    assert len(created_tasks) == 2
    assert all(task.status == TASK_STATUS_PENDING for task in created_tasks)
    assert all(task.parent_task_id == parent.id for task in created_tasks)
    assert all(task.executor_type == PENDING_ENGINE_ROUTING_EXECUTOR for task in created_tasks)
    assert created_tasks[0].sequence_order > source_task.sequence_order


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
        title="Parent task",
        planning_level="high_level",
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )
    source_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Partial atomic task",
        status=TASK_STATUS_PARTIAL,
        sequence_order=1,
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )
    run = make_execution_run(task_id=source_task.id, status="failed")

    decision = make_recovery_decision(
        source_task_id=source_task.id,
        source_run_id=run.id,
        action="manual_review",
        requires_manual_review=True,
        created_tasks=[],
        still_blocks_progress=True,
        reason="The failure requires human intervention before safe continuation.",
        covered_gap_summary="The missing implementation path is ambiguous and should be reviewed.",
    )

    created_tasks = materialize_recovery_decision(
        db_session,
        project_id=project.id,
        decision=decision,
    )

    db_session.refresh(source_task)

    assert created_tasks == []
    assert source_task.status == TASK_STATUS_PARTIAL


def test_retry_action_is_rejected_in_current_workflow():
    with pytest.raises(ValidationError, match="Input should be 'reatomize', 'insert_followup' or 'manual_review'"):
        RecoveryDecision(
            source_task_id=1,
            source_run_id=1,
            action="retry",
            confidence="medium",
            reason="Retry should no longer be allowed.",
            covered_gap_summary="Legacy retry is not supported anymore.",
            retry_same_task=False,
            requires_manual_review=False,
            still_blocks_progress=True,
            created_tasks=[],
            decision_origin="post_batch_recovery",
        )