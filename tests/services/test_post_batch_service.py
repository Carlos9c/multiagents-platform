import pytest

import json

from app.models.artifact import Artifact
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

def test_post_batch_uses_real_checkpoint_artifact_window_and_ignores_older_task_artifacts(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
):
    project = make_project()

    task = make_task(
        project_id=project.id,
        title="Completed task",
        status=TASK_STATUS_COMPLETED,
    )
    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Task completed successfully.",
    )

    old_artifact = make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="old_debug_note",
        content="artifact from an older cycle",
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ]
    )

    new_artifact = make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="code_validation_result",
        content='{"decision":"completed"}',
    )

    captured_kwargs = {}

    evaluation_output = make_stage_evaluation_output(
        decision="stage_completed",
        decision_summary="The final batch satisfied the stage goals and the stage can be closed.",
        project_stage_closed=True,
        stage_goals_satisfied=True,
        recommended_next_action="close_stage",
        recommended_next_action_reason="The stage goals are fully satisfied.",
        decision_signals=["stage_goals_satisfied"],
        plan_change_scope="none",
        remaining_plan_still_valid=True,
        completed_task_ids=[task.id],
        key_risks=[],
        notes=["Close the stage."],
    )

    def _fake_evaluate_checkpoint(**kwargs):
        captured_kwargs.update(kwargs)
        return evaluation_output

    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        _fake_evaluate_checkpoint,
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
        checkpoint_artifact_window_start_exclusive=old_artifact.id,
    )

    assert result.status == "project_stage_closed"
    assert captured_kwargs["checkpoint_artifact_window_ids"] == [new_artifact.id]
    assert old_artifact.id not in captured_kwargs["checkpoint_artifact_window_ids"]

def test_post_batch_persists_resolved_action_and_decision_signals(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_execution_plan,
    make_stage_evaluation_output,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Batch task",
        status=TASK_STATUS_COMPLETED,
    )
    make_execution_run(
        task_id=task.id,
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
                "task_ids": [task.id],
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
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The remaining backlog already represents the correct next work.",
        decision_signals=["remaining_plan_still_valid", "non_blocking_followup_work"],
        completed_task_ids=[task.id],
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

    assert result.resolved_action == "continue_current_plan"
    assert result.decision_signals_used == [
        "remaining_plan_still_valid",
        "non_blocking_followup_work",
    ]
    assert result.continue_execution is True
    assert result.requires_replanning is False
    assert result.requires_resequencing is False
    assert result.requires_manual_review is False

def test_post_batch_continues_when_only_new_recovery_tasks_exist_but_they_are_non_blocking(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_execution_plan,
    make_stage_evaluation_output,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Completed batch task",
        status=TASK_STATUS_COMPLETED,
    )
    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Task completed successfully.",
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task.id],
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
        recommended_next_action_reason="New recovery work is additive and does not block the pending plan.",
        decision_signals=["remaining_plan_still_valid", "non_blocking_followup_work"],
        new_recovery_tasks_blocking=False,
        followup_atomic_tasks_required=False,
        notes=["Continue without resequencing."],
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

    assert result.resolved_action == "continue_current_plan"
    assert result.continue_execution is True
    assert result.requires_replanning is False
    assert result.requires_resequencing is False
    assert result.requires_manual_review is False

def test_post_batch_persists_workflow_iteration_trace_artifact(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_execution_plan,
    make_stage_evaluation_output,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Batch task",
        status=TASK_STATUS_COMPLETED,
    )
    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Task completed successfully.",
    )

    pending_followup = make_task(
        project_id=project.id,
        title="Pending follow-up",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        plan_version=2,
        supersedes_plan_version=1,
        batches=[
            {
                "batch_id": "batch_1",
                "batch_internal_id": "2_1",
                "batch_index": 1,
                "plan_version": 2,
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "batch_internal_id": "2_2",
                "batch_index": 2,
                "plan_version": 2,
                "task_ids": [pending_followup.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The remaining backlog already represents the correct next work.",
        decision_signals=["remaining_plan_still_valid", "non_blocking_followup_work"],
        completed_task_ids=[task.id],
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
        persist_result=True,
    )

    trace_artifact = (
        db_session.query(Artifact)
        .filter(
            Artifact.project_id == project.id,
            Artifact.artifact_type == "workflow_iteration_trace",
        )
        .order_by(Artifact.id.desc())
        .first()
    )

    assert trace_artifact is not None

    payload = json.loads(trace_artifact.content)

    assert payload["project_id"] == project.id
    assert payload["plan_version"] == 2
    assert payload["batch_internal_id"] == "2_1"
    assert payload["batch_id"] == "batch_1"
    assert payload["batch_index"] == 1
    assert payload["checkpoint_id"] == result.checkpoint_id
    assert payload["executed_task_ids"] == [task.id]
    assert payload["successful_task_ids"] == [task.id]
    assert payload["problematic_run_ids"] == []
    assert payload["created_recovery_task_ids"] == []
    assert payload["resolved_action"] == "continue_current_plan"
    assert payload["decision_signals_used"] == [
        "remaining_plan_still_valid",
        "non_blocking_followup_work",
    ]
    assert payload["continue_execution"] is True
    assert payload["requires_replanning"] is False
    assert payload["requires_resequencing"] is False
    assert payload["requires_manual_review"] is False
    assert payload["is_final_batch"] is False

def test_post_batch_trace_persists_recovery_created_task_ids_and_resequence_action(
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

    pending_future_task = make_task(
        project_id=project.id,
        title="Pending future task",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        plan_version=1,
        supersedes_plan_version=None,
        batches=[
            {
                "batch_id": "batch_1",
                "batch_internal_id": "1_1",
                "batch_index": 1,
                "plan_version": 1,
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "batch_internal_id": "1_2",
                "batch_index": 2,
                "plan_version": 1,
                "task_ids": [pending_future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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
        still_blocks_progress=True,
        reason="Recovered work must execute before the remaining pending batch.",
        covered_gap_summary="A new atomic task is required before continuing.",
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recovery_strategy="insert_followup_atomic_tasks",
        recovery_reason="New work must be executed before the pending plan continues.",
        recommended_next_action="resequence_remaining_batches",
        recommended_next_action_reason="Recovered work requires precedence over the remaining batch.",
        decision_signals=["new_work_requires_precedence", "remaining_plan_still_valid"],
        plan_change_scope="local_resequencing",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=True,
        followup_atomic_tasks_required=True,
        failed_task_ids=[failed_task.id],
        notes=["Resequence the remaining plan to execute recovery work first."],
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
        persist_result=True,
    )

    trace_artifact = (
        db_session.query(Artifact)
        .filter(
            Artifact.project_id == project.id,
            Artifact.artifact_type == "workflow_iteration_trace",
        )
        .order_by(Artifact.id.desc())
        .first()
    )

    assert trace_artifact is not None

    payload = json.loads(trace_artifact.content)

    assert payload["batch_internal_id"] == "1_1"
    assert payload["batch_id"] == "batch_1"
    assert payload["resolved_action"] == "resequence_remaining_batches"
    assert payload["decision_signals_used"] == [
        "new_work_requires_precedence",
        "remaining_plan_still_valid",
    ]
    assert payload["requires_resequencing"] is True
    assert payload["requires_replanning"] is False
    assert payload["continue_execution"] is False
    assert payload["problematic_run_ids"] == [run.id]
    assert len(payload["created_recovery_task_ids"]) == 1
    assert payload["created_recovery_task_ids"][0] > 0
    assert result.resolved_action == "resequence_remaining_batches"

def test_post_batch_creates_patch_batch_for_blocking_recovery_work(
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

    pending_future_task = make_task(
        project_id=project.id,
        title="Pending future task",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        plan_version=1,
        supersedes_plan_version=None,
        batches=[
            {
                "batch_id": "plan_1_batch_1",
                "batch_internal_id": "1_1",
                "batch_index": 1,
                "plan_version": 1,
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "plan_1_batch_2",
                "batch_internal_id": "1_2",
                "batch_index": 2,
                "plan_version": 1,
                "task_ids": [pending_future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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
        still_blocks_progress=True,
        reason="Recovered work must execute before the remaining pending batch.",
        covered_gap_summary="A new atomic task is required before continuing.",
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recovery_strategy="insert_followup_atomic_tasks",
        recovery_reason="New work must be executed before the pending plan continues.",
        recommended_next_action="resequence_remaining_batches",
        recommended_next_action_reason="Recovered work requires precedence over the remaining batch.",
        decision_signals=["new_work_requires_precedence", "remaining_plan_still_valid"],
        plan_change_scope="local_resequencing",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=True,
        followup_atomic_tasks_required=True,
        failed_task_ids=[failed_task.id],
        notes=["Insert a patch batch before continuing."],
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
        batch_id="plan_1_batch_1",
        persist_result=False,
    )

    assert result.patched_execution_plan is not None
    assert result.resolved_action == "resequence_remaining_batches"

    patched_batches = result.patched_execution_plan.execution_batches
    assert len(patched_batches) == 3

    patch_batch = patched_batches[1]
    assert patch_batch.is_patch_batch is True
    assert patch_batch.batch_internal_id == "1_1_p1"
    assert patch_batch.name == "Plan 1 · Batch 1.1"
    assert len(patch_batch.task_ids) == 1
    assert patch_batch.task_ids[0] > 0