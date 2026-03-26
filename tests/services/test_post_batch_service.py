import pytest

from app.models.task import (
    PENDING_ENGINE_ROUTING_EXECUTOR,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
)
from app.services.post_batch_service import (
    PostBatchServiceError,
    process_batch_after_execution,
)


def test_post_batch_continues_on_successful_intermediate_checkpoint(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_execution_plan,
    make_stage_evaluation_output,
):
    project = make_project()
    batch_1_task = make_task(
        project_id=project.id,
        title="Batch 1 task",
        status=TASK_STATUS_COMPLETED,
    )
    make_execution_run(
        task_id=batch_1_task.id,
        status="succeeded",
        work_summary="Task completed successfully.",
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [batch_1_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [9999],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ]
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The remaining backlog already represents the correct next work.",
        completed_task_ids=[batch_1_task.id],
        notes=["Continue with the next batch."],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert result.status == "completed_with_evaluation"
    assert result.continue_execution is True
    assert result.requires_replanning is False
    assert result.requires_resequencing is False
    assert result.requires_manual_review is False
    assert result.executed_task_ids == [batch_1_task.id]
    assert result.successful_task_ids == [batch_1_task.id]
    assert result.problematic_run_ids == []

def test_post_batch_requests_resequencing_when_evaluator_recommends_resequence_remaining_batches(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_execution_plan,
    make_stage_evaluation_output,
):
    project = make_project()

    completed_task = make_task(
        project_id=project.id,
        title="Completed task",
        status=TASK_STATUS_COMPLETED,
    )
    make_execution_run(
        task_id=completed_task.id,
        status="succeeded",
        work_summary="Task completed successfully.",
    )

    pending_followup = make_task(
        project_id=project.id,
        title="Pending follow-up",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [completed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [pending_followup.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ]
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        followup_atomic_tasks_required=True,
        followup_atomic_tasks_reason="A non-critical follow-up task should be regrouped with later work.",
        recovery_strategy="insert_followup_atomic_tasks",
        recovery_reason="Recovery introduced local follow-up work without invalidating the overall stage plan.",
        recommended_next_action="resequence_remaining_batches",
        recommended_next_action_reason=(
            "The remaining work is still valid, but regrouping avoids an awkward one-task validation cycle."
        ),
        decision_signals=[
            "remaining_plan_still_valid",
            "followup_tasks_created",
            "single_task_tail_risk",
        ],
        plan_change_scope="local_resequencing",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        single_task_tail_risk=True,
        completed_task_ids=[completed_task.id],
        notes=["Resequence the remaining work."],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert result.status == "checkpoint_blocked"
    assert result.continue_execution is False
    assert result.requires_replanning is False
    assert result.requires_resequencing is True
    assert result.requires_manual_review is False
    assert result.finalization_guard_triggered is False


def test_post_batch_requests_replanning_when_evaluator_recommends_replan_remaining_work(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_execution_plan,
    make_stage_evaluation_output,
):
    project = make_project()

    completed_task = make_task(
        project_id=project.id,
        title="Completed task",
        status=TASK_STATUS_COMPLETED,
    )
    make_execution_run(
        task_id=completed_task.id,
        status="succeeded",
        work_summary="Task completed successfully.",
    )

    pending_future_task = make_task(
        project_id=project.id,
        title="Pending future task",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [completed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [pending_future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ]
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recovery_strategy="replan_from_high_level",
        recovery_reason="The remaining work is no longer represented correctly by the current plan.",
        replan_required=True,
        replan_level="high_level",
        replan_reason="A new structural dependency changes how the remaining stage should be organized.",
        recommended_next_action="replan_remaining_work",
        recommended_next_action_reason=(
            "The remaining batches no longer reflect the correct structure of the work."
        ),
        decision_signals=[
            "structural_gap_detected",
            "high_level_plan_invalid",
        ],
        plan_change_scope="high_level_replan",
        remaining_plan_still_valid=False,
        new_recovery_tasks_blocking=True,
        single_task_tail_risk=False,
        completed_task_ids=[completed_task.id],
        notes=["Replan the remaining work from the high-level stage layer."],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert result.status == "checkpoint_blocked"
    assert result.continue_execution is False
    assert result.requires_replanning is True
    assert result.requires_resequencing is False
    assert result.requires_manual_review is False
    assert result.finalization_guard_triggered is False


def test_post_batch_raises_if_recovery_reopens_source_task_to_pending(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_recovery_decision,
):
    project = make_project()
    failed_task = make_task(
        project_id=project.id,
        title="Failed task",
        status=TASK_STATUS_FAILED,
    )
    run = make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="internal",
        failure_code="executor_failed",
    )
    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="code_validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ]
    )

    decision = make_recovery_decision(
        source_task_id=failed_task.id,
        source_run_id=run.id,
        action="manual_review",
        requires_manual_review=True,
        created_tasks=[],
        still_blocks_progress=True,
        reason="A human decision is required before continuing safely.",
        covered_gap_summary="The task could not be safely completed automatically.",
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )

    def _bad_materialize(*, db, project_id, decision):
        source_task = db.get(type(failed_task), failed_task.id)
        source_task.status = TASK_STATUS_PENDING
        db.add(source_task)
        db.commit()
        return []

    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        _bad_materialize,
    )

    with pytest.raises(PostBatchServiceError, match="Recovery integrity error"):
        process_batch_after_execution(
            db_session,
            project_id=project.id,
            plan=plan,
            batch_id="batch_1",
            persist_result=False,
        )


def test_post_batch_records_recovery_created_tasks_and_reopens_parent(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_recovery_decision,
    make_stage_evaluation_output,
):
    project = make_project()
    parent = make_task(
        project_id=project.id,
        title="Parent task",
        planning_level="high_level",
        status=TASK_STATUS_PENDING,
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )
    failed_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Failed atomic task",
        status=TASK_STATUS_FAILED,
        sequence_order=1,
    )
    run = make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="internal",
        failure_code="executor_failed",
    )
    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="code_validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ]
    )

    decision = make_recovery_decision(
        source_task_id=failed_task.id,
        source_run_id=run.id,
        action="reatomize",
        created_tasks=[
            {
                "title": "Create minimal Python scaffold",
                "description": "Create the minimal implementation files needed to continue the recovered work.",
                "objective": "Seed the implementation surface.",
                "implementation_notes": "Use conventional file names.",
                "acceptance_criteria": "The repo has a minimal executable structure.",
            }
        ],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: make_stage_evaluation_output(
            decision="manual_review_required",
            manual_review_required=True,
            manual_review_reason="Recovered work is pending and requires human checkpoint review.",
            recovery_strategy="manual_review",
            recovery_reason="Recovered work still blocks progress.",
            recommended_next_action="manual_review",
            recommended_next_action_reason="Automatic progression is not trustworthy enough after this recovery step.",
            completed_task_ids=[],
            failed_task_ids=[failed_task.id],
            notes=["Recovery created follow-up tasks."],
        ),
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    db_session.refresh(parent)
    db_session.refresh(failed_task)

    assert failed_task.status == TASK_STATUS_FAILED
    assert parent.status == TASK_STATUS_PENDING
    assert len(result.recovery_context.recovery_created_tasks) == 1
    assert result.recovery_context.recovery_created_tasks[0].source_task_id == failed_task.id
    assert result.problematic_run_ids == [run.id]