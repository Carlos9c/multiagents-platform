import pytest
from pydantic import ValidationError

from app.models.task import (
    PENDING_ENGINE_ROUTING_EXECUTOR,
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_FOLLOWED_UP,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TASK_STATUS_REATOMIZED,
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

    assert source_task.status == TASK_STATUS_REATOMIZED
    assert len(created_tasks) == 2

    for created in created_tasks:
        assert created.project_id == project.id
        assert created.parent_task_id == parent.id
        assert created.planning_level == PLANNING_LEVEL_ATOMIC
        assert created.executor_type == PENDING_ENGINE_ROUTING_EXECUTOR
        assert created.status == TASK_STATUS_PENDING
        assert created.is_recovery_task is True

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


def test_insert_followup_sets_source_task_to_followed_up(
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
        description="Parent task.",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
    )

    source_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Partial atomic task",
        description="This task ended partial and needs a follow-up.",
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
        action="insert_followup",
        created_tasks=[
            {
                "title": "Finish remaining work",
                "description": "Follow-up task covering the remaining gap.",
                "objective": "Cover the remaining gap.",
                "implementation_notes": "Continue from where partial left off.",
                "acceptance_criteria": "The remaining gap is fully covered.",
            }
        ],
        still_blocks_progress=False,
    )

    created_tasks = materialize_recovery_decision(
        db=db_session,
        project_id=project.id,
        decision=decision,
    )

    db_session.refresh(source_task)

    assert source_task.status == TASK_STATUS_FOLLOWED_UP
    assert len(created_tasks) == 1

    followup = created_tasks[0]
    assert followup.project_id == project.id
    assert followup.parent_task_id == parent.id
    assert followup.planning_level == PLANNING_LEVEL_ATOMIC
    assert followup.executor_type == PENDING_ENGINE_ROUTING_EXECUTOR
    assert followup.status == TASK_STATUS_PENDING
    assert followup.is_recovery_task is False


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


def test_insert_followup_on_failed_task_is_promoted_to_reatomize(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_recovery_decision,
):
    """
    A completely failed task has no promotable workspace to follow up on.
    The service must promote insert_followup → reatomize so the task is
    re-specified from scratch rather than issued a follow-up that would start
    from the same empty baseline.
    """
    project = make_project()

    parent = make_task(
        project_id=project.id,
        title="Parent task",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
    )

    source_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Completely failed task",
        description="This task failed in full — nothing was promoted.",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_FAILED,
        sequence_order=1,
    )
    run = make_execution_run(
        task_id=source_task.id,
        status="failed",
        failure_type="validation_failed",
        failure_code="all_validators_failed",
    )

    # Model incorrectly decided insert_followup for a completely failed task.
    decision = make_recovery_decision(
        source_task_id=source_task.id,
        source_run_id=run.id,
        action="insert_followup",
        created_tasks=[
            {
                "title": "Re-attempt the failed work",
                "description": "Follow-up to address everything that failed.",
                "objective": "Complete the originally intended deliverable.",
                "implementation_notes": "Fix all blocking issues found in the failure.",
                "acceptance_criteria": "All validators pass.",
            }
        ],
        still_blocks_progress=True,
    )

    created_tasks = materialize_recovery_decision(
        db=db_session,
        project_id=project.id,
        decision=decision,
    )

    db_session.refresh(source_task)

    # Guard promoted insert_followup → reatomize.
    # Source task must be REATOMIZED (terminal) — not followed_up.
    assert source_task.status == TASK_STATUS_REATOMIZED

    # Created task carries is_recovery_task=True (reatomize semantics).
    assert len(created_tasks) == 1
    reatomized = created_tasks[0]
    assert reatomized.is_recovery_task is True
    assert reatomized.followup_depth == 0
    assert reatomized.status == TASK_STATUS_PENDING
    assert reatomized.parent_task_id == parent.id


def test_insert_followup_on_failed_recovery_task_escalates_to_manual_review(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_recovery_decision,
):
    """
    When a recovery task (is_recovery_task=True) itself fails and the model
    issues insert_followup, the guard promotes it to reatomize, then the
    anti-cascade promotes it further to manual_review — preventing an infinite
    loop where recovery keeps spawning new tasks that keep failing.
    """
    project = make_project()

    parent = make_task(
        project_id=project.id,
        title="Parent task",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
    )

    # A recovery task that has itself failed.
    source_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Failed recovery task",
        description="First recovery attempt that also failed.",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_FAILED,
        is_recovery_task=True,  # this is the key — it is already a recovery task
        sequence_order=2,
    )
    run = make_execution_run(
        task_id=source_task.id,
        status="failed",
        failure_type="validation_failed",
        failure_code="all_validators_failed",
    )

    decision = make_recovery_decision(
        source_task_id=source_task.id,
        source_run_id=run.id,
        action="insert_followup",
        created_tasks=[
            {
                "title": "Second follow-up attempt",
                "description": "Yet another attempt at the same failing scope.",
                "objective": "Complete the work.",
                "implementation_notes": "Try again.",
                "acceptance_criteria": "All validators pass.",
            }
        ],
        still_blocks_progress=True,
    )

    created_tasks = materialize_recovery_decision(
        db=db_session,
        project_id=project.id,
        decision=decision,
    )

    db_session.refresh(source_task)

    # Chain: insert_followup → reatomize (semantic guard) → manual_review (anti-cascade).
    # No new tasks created; source task remains in its FAILED state.
    assert created_tasks == []
    assert source_task.status == TASK_STATUS_FAILED


def test_reatomized_source_allows_parent_to_close_when_recovery_task_completes(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_recovery_decision,
):
    """
    After reatomization the source task is REATOMIZED (terminal, 'good' in hierarchy logic).
    When the recovery task subsequently completes, the parent sees all children in
    {completed, reatomized, followed_up} and consolidates to 'completed'.
    """
    from app.services.task_hierarchy_service import consolidate_parent_task_statuses

    project = make_project()

    parent = make_task(
        project_id=project.id,
        title="Parent task",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
    )

    source_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Failed atomic task",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_FAILED,
        sequence_order=1,
    )
    run = make_execution_run(
        task_id=source_task.id,
        status="failed",
        failure_type="validation_failed",
        failure_code="all_validators_failed",
    )

    decision = make_recovery_decision(
        source_task_id=source_task.id,
        source_run_id=run.id,
        action="reatomize",
        created_tasks=[
            {
                "title": "Re-specified replacement task",
                "description": "Better-specified replacement for the failed task.",
                "objective": "Complete the original deliverable correctly.",
                "implementation_notes": "Fix the enum bug identified in validation.",
                "acceptance_criteria": "All validators pass.",
            }
        ],
        still_blocks_progress=True,
    )

    created_tasks = materialize_recovery_decision(
        db=db_session,
        project_id=project.id,
        decision=decision,
    )

    db_session.refresh(source_task)
    db_session.refresh(parent)

    assert source_task.status == TASK_STATUS_REATOMIZED
    assert len(created_tasks) == 1
    recovery_task = created_tasks[0]
    assert recovery_task.is_recovery_task is True
    assert recovery_task.status == TASK_STATUS_PENDING

    # Parent should be pending because the recovery task is still pending.
    consolidate_parent_task_statuses(db=db_session, parent_task_id=parent.id)
    db_session.refresh(parent)
    assert parent.status == TASK_STATUS_PENDING

    # Simulate the recovery task completing successfully.
    recovery_task.status = TASK_STATUS_COMPLETED
    db_session.add(recovery_task)
    db_session.flush()

    # Now all children are {reatomized, completed} → parent closes as completed.
    consolidate_parent_task_statuses(db=db_session, parent_task_id=parent.id)
    db_session.refresh(parent)
    assert parent.status == TASK_STATUS_COMPLETED
