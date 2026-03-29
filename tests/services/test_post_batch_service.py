import types
import pytest

import json

from app.models.artifact import Artifact
from app.models.task import (
    PENDING_ENGINE_ROUTING_EXECUTOR,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
)
from app.services.post_batch_service import (
    PostBatchServiceError,
    process_batch_after_execution,
)
from app.schemas.recovery import (
    RecoveryContext,
    RecoveryCreatedTaskRecord,
    RecoveryDecisionSummary,
)

from app.services.post_batch_service import (
    _build_validation_context_summary,
)


def _assert_intent(result, *, intent_type: str, mutation_scope: str):
    assert result.resolved_intent_type == intent_type
    assert result.resolved_mutation_scope == mutation_scope


def _assert_trace_payload_uses_canonical_contract(payload: dict):
    assert "resolved_action" not in payload
    assert "decision_signals_used" not in payload
    assert "continue_execution" not in payload
    assert "requires_replanning" not in payload
    assert "requires_resequencing" not in payload


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
    assert result.can_continue_after_application is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
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
    assert result.can_continue_after_application is False
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type == "resequence"
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
    assert result.can_continue_after_application is False
    assert result.resolved_intent_type == "replan"
    assert result.resolved_intent_type != "resequence"
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
        artifact_type="validation_result",
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

    with pytest.raises(
        PostBatchServiceError,
        match=r"Recovery integrity error.*|Recovery integrity error",
    ):
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
        artifact_type="validation_result",
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
    assert (
        result.recovery_context.recovery_created_tasks[0].source_task_id
        == failed_task.id
    )
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
        artifact_type="validation_result",
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

    assert result.resolved_intent_type == "continue"
    assert result.resolved_mutation_scope == "none"
    assert result.decision_signals == [
        "remaining_plan_still_valid",
        "non_blocking_followup_work",
    ]
    assert result.can_continue_after_application is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
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

    assert result.resolved_intent_type == "continue"
    assert result.resolved_mutation_scope == "none"
    assert result.can_continue_after_application is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
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
    _assert_trace_payload_uses_canonical_contract(payload)

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
    assert payload["resolved_intent_type"] == "continue"
    assert payload["resolved_mutation_scope"] == "none"
    assert payload["decision_signals"] == [
        "remaining_plan_still_valid",
        "non_blocking_followup_work",
    ]
    assert payload["can_continue_after_application"] is True
    assert payload["resolved_intent_type"] != "replan"
    assert payload["resolved_intent_type"] != "resequence"
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
        artifact_type="validation_result",
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
        followup_atomic_tasks_reason="A new follow-up task must run before the remaining batch can continue.",
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
    _assert_trace_payload_uses_canonical_contract(payload)

    assert payload["batch_internal_id"] == "1_1"
    assert payload["batch_id"] == "batch_1"
    assert payload["resolved_intent_type"] == "resequence"
    assert payload["resolved_mutation_scope"] == "resequence"
    assert payload["decision_signals"] == [
        "new_work_requires_precedence",
        "remaining_plan_still_valid",
    ]
    assert payload["resolved_intent_type"] == "resequence"
    assert payload["resolved_intent_type"] != "replan"
    assert payload["can_continue_after_application"] is False
    assert payload["problematic_run_ids"] == [run.id]
    assert len(payload["created_recovery_task_ids"]) == 1
    assert payload["created_recovery_task_ids"][0] > 0
    assert result.resolved_intent_type == "resequence"
    assert result.resolved_mutation_scope == "resequence"


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
        artifact_type="validation_result",
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
        followup_atomic_tasks_reason="A new follow-up task must run before the remaining batch can continue.",
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
    assert result.resolved_intent_type == "resequence"
    assert result.resolved_mutation_scope == "resequence"

    patched_batches = result.patched_execution_plan.execution_batches
    assert len(patched_batches) == 3

    patch_batch = patched_batches[1]
    assert patch_batch.is_patch_batch is True
    assert patch_batch.batch_internal_id == "1_1_p1"
    assert patch_batch.name == "Plan 1 · Batch 1.1"
    assert len(patch_batch.task_ids) == 1
    assert patch_batch.task_ids[0] > 0


def test_post_batch_runs_recovery_assignment_when_new_non_blocking_tasks_must_be_assigned(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    failed_task = make_task(
        project_id=project.id,
        title="Task with recovery follow-up",
        status=TASK_STATUS_FAILED,
    )
    next_batch_task = make_task(
        project_id=project.id,
        title="Next batch task",
        status=TASK_STATUS_PENDING,
    )

    make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="validation",
        failure_code="missing_followup",
        work_summary="Task failed but produced enough signal for recovery.",
    )
    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            {
                "batch_id": "plan_2_batch_1",
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "plan_2_batch_2",
                "task_ids": [next_batch_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    created_recovery_task = make_task(
        project_id=project.id,
        title="Recovery-created task",
        status=TASK_STATUS_PENDING,
        parent_task_id=None,
    )

    recovery_decision = make_recovery_decision(
        source_task_id=failed_task.id,
        source_run_id=_get_latest_run_id_for_task(db_session, failed_task.id),
        action="insert_followup",
        still_blocks_progress=False,
        created_tasks=[
            {
                "title": created_recovery_task.title,
                "description": created_recovery_task.description,
                "implementation_notes": created_recovery_task.implementation_notes,
                "acceptance_criteria": created_recovery_task.acceptance_criteria,
                "priority": created_recovery_task.priority,
                "task_type": created_recovery_task.task_type,
            }
        ],
        reason="Recovery created one non-blocking follow-up task.",
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The plan can continue if the new task is assigned first.",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        notes=["Continue, but do not leave the new recovery task unassigned."],
    )

    compiled_plan = make_execution_plan(
        plan_version=2,
        batches=[
            {
                "batch_id": "plan_2_batch_1",
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "plan_2_batch_1_patch_1",
                "batch_internal_id": "2_1_p1",
                "batch_index": 1,
                "plan_version": 2,
                "task_ids": [created_recovery_task.id],
                "checkpoint_id": "checkpoint_plan_2_batch_1_patch_1",
                "checkpoint_name": "Patch checkpoint 1.1",
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "plan_2_batch_2",
                "task_ids": [next_batch_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [created_recovery_task],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="assignment",
            patched_execution_plan=compiled_plan,
            requires_replan=False,
            notes=["All new work was assigned safely."],
            metadata={
                "assigned_task_ids": [created_recovery_task.id],
                "unassigned_task_ids": [],
                "compiled_cluster_assignments": [
                    {
                        "cluster_id": "cluster_1",
                        "task_ids_in_execution_order": [created_recovery_task.id],
                        "impact_type": "additive_deferred",
                        "placement_relation": "after_current_tail",
                        "batch_assignment_mode": "new_patch_batch",
                        "target_batch_id": "plan_2_batch_1_patch_1",
                        "target_batch_name": "Plan 2 · Batch 1.1",
                        "intrabatch_placement_mode": "not_applicable",
                        "anchor_task_id": None,
                        "rationale": "Append the new task as a patch batch.",
                    }
                ],
            },
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="plan_2_batch_1",
        persist_result=True,
    )

    assert result.status == "completed_with_evaluation"
    assert result.can_continue_after_application is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
    assert result.requires_manual_review is False
    assert result.patched_execution_plan is not None
    assert [
        batch.batch_id for batch in result.patched_execution_plan.execution_batches
    ] == [
        "plan_2_batch_1",
        "plan_2_batch_1_patch_1",
        "plan_2_batch_2",
    ]
    assert "Recovery assignment placed all new tasks before continuing" in result.notes

    artifact_types = [
        artifact.artifact_type
        for artifact in db_session.query(Artifact)
        .filter(Artifact.project_id == project.id)
        .order_by(Artifact.id.asc())
        .all()
    ]
    assert "post_batch_result" in artifact_types
    assert "workflow_iteration_trace" in artifact_types


def test_post_batch_final_batch_stays_open_when_recovery_assignment_extends_the_live_plan(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    failed_final_task = make_task(
        project_id=project.id,
        title="Final batch task with recovery follow-up",
        status=TASK_STATUS_FAILED,
    )

    make_execution_run(
        task_id=failed_final_task.id,
        status="failed",
        failure_type="validation",
        failure_code="needs_followup",
        work_summary="The final batch surfaced one remaining follow-up task.",
    )
    make_artifact(
        project_id=project.id,
        task_id=failed_final_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        plan_version=3,
        batches=[
            {
                "batch_id": "plan_3_batch_1",
                "task_ids": [failed_final_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )

    created_recovery_task = make_task(
        project_id=project.id,
        title="Recovery-created tail task",
        status=TASK_STATUS_PENDING,
    )

    recovery_decision = make_recovery_decision(
        source_task_id=failed_final_task.id,
        source_run_id=_get_latest_run_id_for_task(db_session, failed_final_task.id),
        action="insert_followup",
        still_blocks_progress=False,
        created_tasks=[
            {
                "title": created_recovery_task.title,
                "description": created_recovery_task.description,
                "implementation_notes": created_recovery_task.implementation_notes,
                "acceptance_criteria": created_recovery_task.acceptance_criteria,
                "priority": created_recovery_task.priority,
                "task_type": created_recovery_task.task_type,
            }
        ],
        reason="Recovery created one additional task after the previously final batch.",
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason=(
            "The original final batch no longer closes the stage because the new recovery work "
            "must be assigned before completion."
        ),
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        notes=["Recovery introduced new pending work, so the stage must remain open."],
    )

    compiled_plan = make_execution_plan(
        plan_version=3,
        batches=[
            {
                "batch_id": "plan_3_batch_1",
                "task_ids": [failed_final_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "plan_3_batch_1_patch_1",
                "batch_internal_id": "3_1_p1",
                "batch_index": 1,
                "plan_version": 3,
                "task_ids": [created_recovery_task.id],
                "checkpoint_id": "checkpoint_plan_3_batch_1_patch_1",
                "checkpoint_name": "Patch checkpoint 1.1",
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [created_recovery_task],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="assignment",
            patched_execution_plan=compiled_plan,
            requires_replan=False,
            notes=["All new work was assigned safely."],
            metadata={
                "assigned_task_ids": [created_recovery_task.id],
                "unassigned_task_ids": [],
                "compiled_cluster_assignments": [
                    {
                        "cluster_id": "cluster_1",
                        "task_ids_in_execution_order": [created_recovery_task.id],
                        "impact_type": "additive_deferred",
                        "placement_relation": "after_current_tail",
                        "batch_assignment_mode": "new_patch_batch",
                        "target_batch_id": "plan_2_batch_1_patch_1",
                        "target_batch_name": "Plan 2 · Batch 1.1",
                        "intrabatch_placement_mode": "not_applicable",
                        "anchor_task_id": None,
                        "rationale": "Append the new task as a patch batch.",
                    }
                ],
            },
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="plan_3_batch_1",
        persist_result=False,
    )

    assert result.is_final_batch is True
    assert result.status == "finalization_reopened"
    assert result.reopened_finalization is True
    assert result.can_continue_after_application is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
    assert result.patched_execution_plan is not None
    assert [
        batch.batch_id for batch in result.patched_execution_plan.execution_batches
    ] == [
        "plan_3_batch_1",
        "plan_3_batch_1_patch_1",
    ]
    assert "original final batch no longer closes the stage" in result.notes


def _get_latest_run_id_for_task(db_session, task_id: int) -> int:
    from app.models.execution_run import ExecutionRun

    run = (
        db_session.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )
    assert run is not None
    return run.id


def test_post_batch_consumes_assignment_mutation_result_from_live_plan_mutation_service(
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

    failed_task = make_task(
        project_id=project.id,
        title="Failed task",
        status=TASK_STATUS_FAILED,
    )
    make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="validation",
        failure_code="needs_followup",
        work_summary="Task failed and produced recovery follow-up work.",
    )
    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    created_recovery_task = make_task(
        project_id=project.id,
        title="Recovery task",
        status=TASK_STATUS_PENDING,
    )

    pending_task = make_task(
        project_id=project.id,
        title="Pending task",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [pending_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ]
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The new recovery work must be assigned into the active plan.",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        notes=["Continue after assigning new work."],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: types.SimpleNamespace(
            source_task_id=failed_task.id,
            source_run_id=1,
            action="insert_followup",
            reason="Create follow-up work.",
            still_blocks_progress=False,
        ),
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [created_recovery_task],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.build_recovery_context_entry",
        lambda **kwargs: RecoveryContext(
            recovery_decisions=[
                RecoveryDecisionSummary(
                    source_task_id=failed_task.id,
                    source_run_id=1,
                    action="insert_followup",
                    confidence="medium",
                    reason="Create follow-up work.",
                    still_blocks_progress=False,
                    created_task_ids=[created_recovery_task.id],
                )
            ],
            recovery_created_tasks=[
                RecoveryCreatedTaskRecord(
                    created_task_id=created_recovery_task.id,
                    source_task_id=failed_task.id,
                    source_run_id=1,
                    title=created_recovery_task.title,
                    planning_level=created_recovery_task.planning_level,
                    executor_type=created_recovery_task.executor_type,
                )
            ],
        ),
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.merge_recovery_contexts",
        lambda contexts: contexts[0]
        if contexts
        else RecoveryContext(
            recovery_decisions=[],
            open_issues=[],
            recovery_created_tasks=[],
        ),
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.resolve_post_batch_intent",
        lambda signals: types.SimpleNamespace(
            intent_type="assign",
            mutation_scope="assignment",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=True,
            requires_plan_mutation=True,
            requires_all_new_tasks_assigned=True,
            can_continue_after_application=True,
            should_close_stage=False,
            requires_manual_review=False,
            reopened_finalization=False,
            notes="New recovery work must be assigned into the active plan before the next batch starts.",
            decision_signals=list(getattr(signals, "decision_signals", [])),
        ),
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="assignment",
            patched_execution_plan=plan,
            requires_replan=False,
            notes=["Assigned successfully."],
            metadata={
                "assigned_task_ids": [created_recovery_task.id],
                "compiled_cluster_assignments": [{"cluster_id": "cluster_1"}],
            },
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert result.can_continue_after_application is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
    assert result.patched_execution_plan is not None
    assert f"assigned_task_ids=[{created_recovery_task.id}]" in result.notes


def test_post_batch_does_not_materialize_patch_for_deferred_resequence(
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
        status="completed",
    )
    make_execution_run(
        task_id=completed_task.id,
        status="succeeded",
        work_summary="Task completed successfully.",
    )

    pending_followup = make_task(
        project_id=project.id,
        title="Pending follow-up",
        status="pending",
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
        followup_atomic_tasks_reason=(
            "Recovery introduced follow-up atomic work that should be resequenced "
            "without rebuilding the remaining plan."
        ),
        recovery_strategy="insert_followup_atomic_tasks",
        recovery_reason=(
            "Recovery introduced local follow-up work without invalidating the overall stage plan."
        ),
        recommended_next_action="resequence_remaining_batches",
        recommended_next_action_reason="The remaining work should be regrouped.",
        decision_signals=[
            "remaining_plan_still_valid",
            "followup_tasks_created",
            "single_task_tail_risk",
        ],
        plan_change_scope="local_resequencing",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        single_task_tail_risk=True,
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
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="resequence_deferred",
            patched_execution_plan=None,
            requires_replan=False,
            notes=["Deferred resequence; no immediate patch batch was created."],
            metadata={},
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert result.resolved_intent_type == "resequence"
    assert result.resolved_intent_type != "replan"
    assert result.can_continue_after_application is False
    assert result.patched_execution_plan is None


def test_post_batch_raises_on_contradictory_signals_from_untrusted_evaluator_payload(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
):
    project = make_project()

    task = make_task(
        project_id=project.id,
        title="Task with contradictory evaluation",
        status=TASK_STATUS_COMPLETED,
    )

    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Task completed, but evaluator emitted contradictory signals.",
    )

    make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="validation_result",
        content='{"decision":"completed"}',
    )

    plan = make_execution_plan(
        plan_version=10,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )

    raw_contradictory_output = types.SimpleNamespace(
        decision="stage_incomplete",
        decision_summary="Contradictory output",
        stage_goals_satisfied=False,
        project_stage_closed=False,
        recovery_strategy="none",
        recovery_reason=None,
        replan=types.SimpleNamespace(
            required=True,
            level="high_level",
            reason="Contradictory replanning",
            target_task_ids=[],
        ),
        followup_atomic_tasks_required=False,
        followup_atomic_tasks_reason=None,
        manual_review_required=False,
        manual_review_reason=None,
        recommended_next_action="replan_remaining_work",
        recommended_next_action_reason="Contradictory evaluator output.",
        decision_signals=["remaining_plan_still_valid", "force_replan"],
        plan_change_scope="high_level_replan",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        single_task_tail_risk=False,
        evaluated_batches=[],
        key_risks=[],
        notes=[],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: raw_contradictory_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )

    with pytest.raises(PostBatchServiceError):
        process_batch_after_execution(
            db_session,
            project_id=project.id,
            plan=plan,
            batch_id="batch_1",
            persist_result=False,
        )


def test_post_batch_blocking_recovery_forces_stop_or_resequence(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    task = make_task(
        project_id=project.id,
        title="Failed task that creates blocking recovery",
        status=TASK_STATUS_FAILED,
    )

    run = make_execution_run(
        task_id=task.id,
        status="failed",
        failure_type="validation",
        failure_code="missing_dependency",
        work_summary="Task failed because a prerequisite is missing.",
    )

    make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    future_task = make_task(
        project_id=project.id,
        title="Future task after blocking dependency",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        plan_version=11,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    blocking_task = make_task(
        project_id=project.id,
        title="Blocking recovery task",
        status=TASK_STATUS_PENDING,
    )

    recovery_decision = make_recovery_decision(
        source_task_id=task.id,
        source_run_id=run.id,
        action="insert_followup",
        still_blocks_progress=True,
        created_tasks=[
            {
                "title": blocking_task.title,
                "description": blocking_task.description,
                "objective": blocking_task.objective,
                "implementation_notes": blocking_task.implementation_notes,
                "acceptance_criteria": blocking_task.acceptance_criteria,
            }
        ],
        reason="A missing prerequisite must be implemented before progress can continue.",
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="resequence_remaining_batches",
        recommended_next_action_reason=(
            "The plan can continue only after inserting the blocking prerequisite before the next useful work."
        ),
        decision_signals=[
            "remaining_plan_still_valid",
            "followup_tasks_created",
            "new_recovery_tasks_blocking",
        ],
        plan_change_scope="local_resequencing",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=True,
        followup_atomic_tasks_required=True,
        followup_atomic_tasks_reason="A blocking follow-up task must be inserted before the next remaining batch.",
        notes=["Blocking recovery must stop normal continuation."],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [blocking_task],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="assignment",
            patched_execution_plan=plan,
            requires_replan=False,
            notes=[
                "Blocking recovery was assigned, but normal continuation must stop for resequencing."
            ],
            metadata={
                "assigned_task_ids": [blocking_task.id],
                "unassigned_task_ids": [],
                "compiled_cluster_assignments": [
                    {
                        "cluster_id": "blocking_cluster",
                        "task_ids_in_execution_order": [blocking_task.id],
                    }
                ],
            },
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert result.can_continue_after_application is False
    assert result.resolved_intent_type == "resequence"
    assert result.resolved_intent_type != "replan"


def test_post_batch_invalid_plan_without_recovery_forces_replan(
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
        title="Completed task with invalidated remaining plan",
        status=TASK_STATUS_COMPLETED,
    )

    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Task completed successfully, but the remaining plan is no longer valid.",
    )

    make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="validation_result",
        content='{"decision":"completed"}',
    )

    plan = make_execution_plan(
        plan_version=12,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="replan_remaining_work",
        recommended_next_action_reason=(
            "No recovery tasks were created, but the remaining plan structure is invalid and must be regenerated."
        ),
        replan_required=True,
        replan_level="high_level",
        replan_reason=(
            "No recovery tasks were created, but the remaining plan structure is invalid and must be regenerated."
        ),
        plan_change_scope="high_level_replan",
        remaining_plan_still_valid=False,
        notes=["Structural invalidation without recovery."],
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

    assert result.resolved_intent_type == "replan"
    assert result.can_continue_after_application is False


def test_make_stage_evaluation_output_requires_canonical_high_level_replan_signals(
    make_stage_evaluation_output,
):
    output = make_stage_evaluation_output(
        decision="stage_incomplete",
        recommended_next_action="replan_remaining_work",
        recommended_next_action_reason="High-level replan is required.",
        replan_required=True,
        replan_level="high_level",
        replan_reason="High-level replan is required.",
        plan_change_scope="high_level_replan",
        remaining_plan_still_valid=False,
    )

    assert output.replan.required is True
    assert output.replan.level == "high_level"
    assert output.plan_change_scope == "high_level_replan"
    assert output.remaining_plan_still_valid is False


def test_post_batch_assigns_multiple_recovery_clusters_without_replan(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    failed_task = make_task(
        project_id=project.id,
        title="Failed implementation task",
        status=TASK_STATUS_FAILED,
    )
    partial_task = make_task(
        project_id=project.id,
        title="Partial validation task",
        status=TASK_STATUS_PARTIAL,
    )
    pending_future_task = make_task(
        project_id=project.id,
        title="Preexisting pending future task",
        status=TASK_STATUS_PENDING,
    )

    failed_run = make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="validation",
        failure_code="missing_edge_case",
        work_summary="The main implementation failed and needs follow-up work.",
    )
    partial_run = make_execution_run(
        task_id=partial_task.id,
        status="partial",
        failure_type="validation",
        failure_code="needs_cleanup",
        work_summary="The implementation is partial and needs one extra cleanup task.",
    )

    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )
    make_artifact(
        project_id=project.id,
        task_id=partial_task.id,
        artifact_type="validation_result",
        content='{"decision":"partial"}',
    )

    plan = make_execution_plan(
        plan_version=4,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [failed_task.id, partial_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [pending_future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    created_recovery_task_a = make_task(
        project_id=project.id,
        title="Recovery cluster A task",
        status=TASK_STATUS_PENDING,
    )
    created_recovery_task_b = make_task(
        project_id=project.id,
        title="Recovery cluster B task",
        status=TASK_STATUS_PENDING,
    )

    recovery_decisions_by_run_id = {
        failed_run.id: make_recovery_decision(
            source_task_id=failed_task.id,
            source_run_id=failed_run.id,
            action="insert_followup",
            still_blocks_progress=False,
            created_tasks=[
                {
                    "title": created_recovery_task_a.title,
                    "description": created_recovery_task_a.description,
                    "objective": created_recovery_task_a.objective,
                    "implementation_notes": created_recovery_task_a.implementation_notes,
                    "acceptance_criteria": created_recovery_task_a.acceptance_criteria,
                }
            ],
            reason="Recovery task A covers the failed gap without invalidating the plan.",
        ),
        partial_run.id: make_recovery_decision(
            source_task_id=partial_task.id,
            source_run_id=partial_run.id,
            action="insert_followup",
            still_blocks_progress=False,
            created_tasks=[
                {
                    "title": created_recovery_task_b.title,
                    "description": created_recovery_task_b.description,
                    "objective": created_recovery_task_b.objective,
                    "implementation_notes": created_recovery_task_b.implementation_notes,
                    "acceptance_criteria": created_recovery_task_b.acceptance_criteria,
                }
            ],
            reason="Recovery task B covers the partial cleanup without invalidating the plan.",
        ),
    }

    materialized_by_source_task_id = {
        failed_task.id: [created_recovery_task_a],
        partial_task.id: [created_recovery_task_b],
    }

    patched_plan = make_execution_plan(
        plan_version=4,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [failed_task.id, partial_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_1_patch_1",
                "batch_internal_id": "4_1_p1",
                "batch_index": 1,
                "plan_version": 4,
                "task_ids": [created_recovery_task_a.id, created_recovery_task_b.id],
                "checkpoint_id": "checkpoint_batch_1_patch_1",
                "checkpoint_name": "Patch checkpoint 1.1",
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [pending_future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The remaining plan is still valid and the new recovery work should be assigned into it.",
        decision_signals=[
            "remaining_plan_still_valid",
            "followup_tasks_created",
            "multi_recovery_cluster",
        ],
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        notes=["Two non-blocking recovery clusters were created."],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decisions_by_run_id[kwargs["run_id"]],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: materialized_by_source_task_id[
            kwargs["decision"].source_task_id
        ],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="assignment",
            patched_execution_plan=patched_plan,
            requires_replan=False,
            notes=["All recovery clusters were assigned without structural changes."],
            metadata={
                "assigned_task_ids": [
                    created_recovery_task_a.id,
                    created_recovery_task_b.id,
                ],
                "unassigned_task_ids": [],
                "compiled_cluster_assignments": [
                    {
                        "cluster_id": "cluster_a",
                        "task_ids_in_execution_order": [created_recovery_task_a.id],
                    },
                    {
                        "cluster_id": "cluster_b",
                        "task_ids_in_execution_order": [created_recovery_task_b.id],
                    },
                ],
            },
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert result.status == "completed_with_evaluation"
    assert result.can_continue_after_application is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
    assert sorted(result.problematic_run_ids) == sorted([failed_run.id, partial_run.id])
    assert sorted(
        record.created_task_id
        for record in result.recovery_context.recovery_created_tasks
    ) == sorted([created_recovery_task_a.id, created_recovery_task_b.id])
    assert result.patched_execution_plan is not None
    assert [
        batch.batch_id for batch in result.patched_execution_plan.execution_batches
    ] == [
        "batch_1",
        "batch_1_patch_1",
        "batch_2",
    ]
    assert (
        f"assigned_task_ids=[{created_recovery_task_a.id}, {created_recovery_task_b.id}]"
        in result.notes
    )


def test_post_batch_final_batch_with_non_blocking_multi_recovery_stays_open_after_assignment(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    failed_final_task = make_task(
        project_id=project.id,
        title="Final task that spawns follow-up work",
        status=TASK_STATUS_FAILED,
    )
    failed_run = make_execution_run(
        task_id=failed_final_task.id,
        status="failed",
        failure_type="validation",
        failure_code="followup_required",
        work_summary="The previous final batch surfaced extra follow-up work.",
    )
    make_artifact(
        project_id=project.id,
        task_id=failed_final_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        plan_version=6,
        batches=[
            {
                "batch_id": "batch_final",
                "task_ids": [failed_final_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )

    recovery_tail_a = make_task(
        project_id=project.id,
        title="Follow-up tail A",
        status=TASK_STATUS_PENDING,
    )
    recovery_tail_b = make_task(
        project_id=project.id,
        title="Follow-up tail B",
        status=TASK_STATUS_PENDING,
    )

    recovery_decision = make_recovery_decision(
        source_task_id=failed_final_task.id,
        source_run_id=failed_run.id,
        action="insert_followup",
        still_blocks_progress=False,
        created_tasks=[
            {
                "title": recovery_tail_a.title,
                "description": recovery_tail_a.description,
                "objective": recovery_tail_a.objective,
                "implementation_notes": recovery_tail_a.implementation_notes,
                "acceptance_criteria": recovery_tail_a.acceptance_criteria,
            },
            {
                "title": recovery_tail_b.title,
                "description": recovery_tail_b.description,
                "objective": recovery_tail_b.objective,
                "implementation_notes": recovery_tail_b.implementation_notes,
                "acceptance_criteria": recovery_tail_b.acceptance_criteria,
            },
        ],
        reason="The former final batch revealed two additive follow-up tasks.",
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The original final batch no longer closes the stage because the new recovery work must be executed first.",
        decision_signals=[
            "remaining_plan_still_valid",
            "final_batch_reopened_by_recovery",
            "followup_tasks_created",
        ],
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        notes=["The stage must remain open until the new tail batches are evaluated."],
    )

    patched_plan = make_execution_plan(
        plan_version=6,
        batches=[
            {
                "batch_id": "batch_final",
                "task_ids": [failed_final_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_final_patch_1",
                "batch_internal_id": "6_1_p1",
                "batch_index": 1,
                "plan_version": 6,
                "task_ids": [recovery_tail_a.id, recovery_tail_b.id],
                "checkpoint_id": "checkpoint_batch_final_patch_1",
                "checkpoint_name": "Patch checkpoint final.1",
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [recovery_tail_a, recovery_tail_b],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="assignment",
            patched_execution_plan=patched_plan,
            requires_replan=False,
            notes=["The new tail work was appended as a patch batch."],
            metadata={
                "assigned_task_ids": [recovery_tail_a.id, recovery_tail_b.id],
                "unassigned_task_ids": [],
                "compiled_cluster_assignments": [
                    {
                        "cluster_id": "tail_cluster",
                        "task_ids_in_execution_order": [
                            recovery_tail_a.id,
                            recovery_tail_b.id,
                        ],
                    }
                ],
            },
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_final",
        persist_result=False,
    )

    assert result.is_final_batch is True
    assert result.status == "finalization_reopened"
    assert result.reopened_finalization is True
    assert result.can_continue_after_application is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
    assert result.requires_manual_review is False
    assert result.patched_execution_plan is not None
    assert [
        batch.batch_id for batch in result.patched_execution_plan.execution_batches
    ] == [
        "batch_final",
        "batch_final_patch_1",
    ]
    assert "The original final batch no longer closes the stage" in result.notes


def test_post_batch_escalates_to_replan_when_assignment_is_only_partial(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    failed_task = make_task(
        project_id=project.id,
        title="Failed task that creates mixed recovery work",
        status=TASK_STATUS_FAILED,
    )
    pending_future_task = make_task(
        project_id=project.id,
        title="Future task",
        status=TASK_STATUS_PENDING,
    )

    failed_run = make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="validation",
        failure_code="split_followup",
    )
    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        plan_version=5,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [pending_future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    assignable_recovery_task = make_task(
        project_id=project.id,
        title="Assignable recovery task",
        status=TASK_STATUS_PENDING,
    )
    structural_recovery_task = make_task(
        project_id=project.id,
        title="Structural recovery task",
        status=TASK_STATUS_PENDING,
    )

    recovery_decision = make_recovery_decision(
        source_task_id=failed_task.id,
        source_run_id=failed_run.id,
        action="insert_followup",
        still_blocks_progress=False,
        created_tasks=[
            {
                "title": assignable_recovery_task.title,
                "description": assignable_recovery_task.description,
                "objective": assignable_recovery_task.objective,
                "implementation_notes": assignable_recovery_task.implementation_notes,
                "acceptance_criteria": assignable_recovery_task.acceptance_criteria,
            },
            {
                "title": structural_recovery_task.title,
                "description": structural_recovery_task.description,
                "objective": structural_recovery_task.objective,
                "implementation_notes": structural_recovery_task.implementation_notes,
                "acceptance_criteria": structural_recovery_task.acceptance_criteria,
            },
        ],
        reason="One new task can be placed locally, but another reveals a structural conflict.",
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="Try to assign the new recovery work first; escalate only if the assignment compiler cannot place everything.",
        decision_signals=["remaining_plan_still_valid", "followup_tasks_created"],
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        notes=["The plan is still valid unless assignment fails."],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [assignable_recovery_task, structural_recovery_task],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="escalated_to_replan",
            patched_execution_plan=None,
            requires_replan=True,
            notes=["The structural recovery task could not be assigned safely."],
            metadata={
                "assigned_task_ids": [assignable_recovery_task.id],
                "unassigned_task_ids": [structural_recovery_task.id],
            },
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert result.status == "checkpoint_blocked"
    assert result.can_continue_after_application is False
    assert result.resolved_intent_type == "replan"
    assert result.resolved_intent_type != "resequence"
    assert result.requires_manual_review is False
    assert result.patched_execution_plan is None
    assert result.resolved_mutation_scope == "replan"
    assert "assignment escalated to replanning" in result.notes


def test_post_batch_raises_when_assignment_intent_does_not_produce_a_patch_plan(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    failed_task = make_task(
        project_id=project.id,
        title="Failed task",
        status=TASK_STATUS_FAILED,
    )
    future_task = make_task(
        project_id=project.id,
        title="Future task",
        status=TASK_STATUS_PENDING,
    )

    failed_run = make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="validation",
        failure_code="missing_patch",
    )
    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        plan_version=7,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    created_recovery_task = make_task(
        project_id=project.id,
        title="Recovery task that must be assigned",
        status=TASK_STATUS_PENDING,
    )

    recovery_decision = make_recovery_decision(
        source_task_id=failed_task.id,
        source_run_id=failed_run.id,
        action="insert_followup",
        still_blocks_progress=False,
        created_tasks=[
            {
                "title": created_recovery_task.title,
                "description": created_recovery_task.description,
                "objective": created_recovery_task.objective,
                "implementation_notes": created_recovery_task.implementation_notes,
                "acceptance_criteria": created_recovery_task.acceptance_criteria,
            }
        ],
        reason="The new task must be assigned into the live plan before continuing.",
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The live plan can continue after assigning the new recovery task.",
        decision_signals=["remaining_plan_still_valid", "followup_tasks_created"],
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [created_recovery_task],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="assignment",
            patched_execution_plan=None,
            requires_replan=False,
            notes=["Buggy mutation path returned no patched plan."],
            metadata={
                "assigned_task_ids": [created_recovery_task.id],
                "unassigned_task_ids": [],
                "compiled_cluster_assignments": [{"cluster_id": "cluster_1"}],
            },
        ),
    )

    with pytest.raises(
        PostBatchServiceError,
        match="required assignment of all new recovery tasks, but no patched execution plan was produced",
    ):
        process_batch_after_execution(
            db_session,
            project_id=project.id,
            plan=plan,
            batch_id="batch_1",
            persist_result=False,
        )


def test_post_batch_assign_intent_escalates_to_replan_when_mutation_cannot_place_tasks(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    failed_task = make_task(
        project_id=project.id,
        title="Failed task requiring follow-up",
        status=TASK_STATUS_FAILED,
    )
    future_task = make_task(
        project_id=project.id,
        title="Future task",
        status=TASK_STATUS_PENDING,
    )

    failed_run = make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="validation",
        failure_code="new_dependency_cluster",
        work_summary="The task failed and produced follow-up work that should be assigned if possible.",
    )

    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        plan_version=13,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    recovery_task = make_task(
        project_id=project.id,
        title="Recovery task that should be assigned",
        status=TASK_STATUS_PENDING,
    )

    recovery_decision = make_recovery_decision(
        source_task_id=failed_task.id,
        source_run_id=failed_run.id,
        action="insert_followup",
        still_blocks_progress=False,
        created_tasks=[
            {
                "title": recovery_task.title,
                "description": recovery_task.description,
                "objective": recovery_task.objective,
                "implementation_notes": recovery_task.implementation_notes,
                "acceptance_criteria": recovery_task.acceptance_criteria,
            }
        ],
        reason="The new work should be inserted locally if the live plan can absorb it.",
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason=(
            "The remaining plan is still valid and the new recovery task should be assigned into it."
        ),
        decision_signals=["remaining_plan_still_valid", "followup_tasks_created"],
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        notes=["Assignment should be attempted before considering replanning."],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [recovery_task],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="escalated_to_replan",
            patched_execution_plan=None,
            requires_replan=True,
            notes=[
                "The recovery task could not be placed safely into the remaining plan."
            ],
            metadata={
                "assigned_task_ids": [],
                "unassigned_task_ids": [recovery_task.id],
            },
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert result.status == "checkpoint_blocked"
    assert result.can_continue_after_application is False
    assert result.resolved_intent_type == "replan"
    assert result.resolved_intent_type != "resequence"
    assert result.requires_manual_review is False
    assert result.patched_execution_plan is None
    assert result.resolved_mutation_scope == "replan"


def test_post_batch_manual_review_intent_overrides_assignment_and_blocks_continuation(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    failed_task = make_task(
        project_id=project.id,
        title="Failed task requiring human judgment",
        status=TASK_STATUS_FAILED,
    )
    future_task = make_task(
        project_id=project.id,
        title="Future task",
        status=TASK_STATUS_PENDING,
    )

    failed_run = make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="validation",
        failure_code="ambiguous_fix",
        work_summary="The task failed in a way that generated follow-up work, but the evaluator is not confident enough to continue automatically.",
    )

    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        plan_version=14,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    recovery_task = make_task(
        project_id=project.id,
        title="Recovery task that could be assigned",
        status=TASK_STATUS_PENDING,
    )

    recovery_decision = make_recovery_decision(
        source_task_id=failed_task.id,
        source_run_id=failed_run.id,
        action="insert_followup",
        still_blocks_progress=False,
        created_tasks=[
            {
                "title": recovery_task.title,
                "description": recovery_task.description,
                "objective": recovery_task.objective,
                "implementation_notes": recovery_task.implementation_notes,
                "acceptance_criteria": recovery_task.acceptance_criteria,
            }
        ],
        reason="Follow-up work exists, but the evaluator is not confident enough to proceed automatically.",
    )

    evaluation_output = make_stage_evaluation_output(
        decision="manual_review_required",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="manual_review",
        recommended_next_action_reason=(
            "The situation is ambiguous and requires human review before continuing."
        ),
        manual_review_required=True,
        manual_review_reason=(
            "The situation is ambiguous and requires human review before continuing."
        ),
        decision_signals=[
            "followup_tasks_created",
            "manual_review_required",
        ],
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        notes=["Manual review must override automatic continuation."],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [recovery_task],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="assignment",
            patched_execution_plan=plan,
            requires_replan=False,
            notes=[
                "The recovery task could be assigned, but assignment must not override manual review."
            ],
            metadata={
                "assigned_task_ids": [recovery_task.id],
                "unassigned_task_ids": [],
                "compiled_cluster_assignments": [
                    {
                        "cluster_id": "manual_review_cluster",
                        "task_ids_in_execution_order": [recovery_task.id],
                    }
                ],
            },
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert result.status == "checkpoint_blocked"
    assert result.can_continue_after_application is False
    assert result.requires_manual_review is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"


def test_post_batch_uses_live_plan_mutation_service_for_recovery_assignment(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    failed_task = make_task(
        project_id=project.id,
        title="Failed task",
        status=TASK_STATUS_FAILED,
    )
    future_task = make_task(
        project_id=project.id,
        title="Future task",
        status=TASK_STATUS_PENDING,
    )

    failed_run = make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="validation",
        failure_code="needs_followup",
        work_summary="The failed task creates recovery work that should be assigned into the live plan.",
    )

    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        plan_version=30,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    recovery_task = make_task(
        project_id=project.id,
        title="Recovery task",
        status=TASK_STATUS_PENDING,
    )

    recovery_decision = make_recovery_decision(
        source_task_id=failed_task.id,
        source_run_id=failed_run.id,
        action="insert_followup",
        still_blocks_progress=False,
        created_tasks=[
            {
                "title": recovery_task.title,
                "description": recovery_task.description,
                "objective": recovery_task.objective,
                "implementation_notes": recovery_task.implementation_notes,
                "acceptance_criteria": recovery_task.acceptance_criteria,
            }
        ],
        reason="Recovery task should be placed through the canonical mutation path.",
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The recovery task should be assigned into the current plan.",
        decision_signals=["remaining_plan_still_valid", "followup_tasks_created"],
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        notes=["Canonical live mutation path should be used."],
    )

    mutation_calls = []

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [recovery_task],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )

    def _fake_mutate_live_plan(**kwargs):
        mutation_calls.append(kwargs)
        return types.SimpleNamespace(
            mutation_kind="assignment",
            patched_execution_plan=plan,
            requires_replan=False,
            notes=["Mutation service handled assignment."],
            metadata={
                "assigned_task_ids": [recovery_task.id],
                "unassigned_task_ids": [],
                "compiled_cluster_assignments": [
                    {
                        "cluster_id": "cluster_1",
                        "task_ids_in_execution_order": [recovery_task.id],
                    }
                ],
            },
        )

    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        _fake_mutate_live_plan,
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_1",
        persist_result=False,
    )

    assert len(mutation_calls) == 1
    assert mutation_calls[0]["plan"].plan_version == 30
    assert mutation_calls[0]["batch"].batch_id == "batch_1"
    assert result.resolved_intent_type != "replan"
    assert result.patched_execution_plan is not None


def test_post_batch_trace_includes_mutation_and_recovery_link_fields(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
    make_recovery_decision,
):
    project = make_project()

    failed_task = make_task(
        project_id=project.id,
        title="Failed task",
        status=TASK_STATUS_FAILED,
    )
    future_task = make_task(
        project_id=project.id,
        title="Future task",
        status=TASK_STATUS_PENDING,
    )

    failed_run = make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="validation",
        failure_code="needs_followup",
        work_summary="Failed task created recovery work.",
    )

    make_artifact(
        project_id=project.id,
        task_id=failed_task.id,
        artifact_type="validation_result",
        content='{"decision":"failed"}',
    )

    plan = make_execution_plan(
        plan_version=40,
        batches=[
            {
                "batch_id": "batch_1",
                "batch_internal_id": "40_1",
                "batch_index": 1,
                "plan_version": 40,
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "batch_internal_id": "40_2",
                "batch_index": 2,
                "plan_version": 40,
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    recovery_task = make_task(
        project_id=project.id,
        title="Recovery task",
        status=TASK_STATUS_PENDING,
    )

    recovery_decision = make_recovery_decision(
        source_task_id=failed_task.id,
        source_run_id=failed_run.id,
        action="insert_followup",
        still_blocks_progress=False,
        created_tasks=[
            {
                "title": recovery_task.title,
                "description": recovery_task.description,
                "objective": recovery_task.objective,
                "implementation_notes": recovery_task.implementation_notes,
                "acceptance_criteria": recovery_task.acceptance_criteria,
            }
        ],
        reason="Recovery work should be assigned into the current plan.",
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="Assign the new recovery task into the current plan.",
        decision_signals=["remaining_plan_still_valid", "followup_tasks_created"],
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        notes=["Assignment should proceed through live plan mutation."],
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        lambda **kwargs: recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [recovery_task],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: __import__("types").SimpleNamespace(
            mutation_kind="assignment",
            patched_execution_plan=plan,
            requires_replan=False,
            notes=["Assigned through live mutation."],
            metadata={
                "assigned_task_ids": [recovery_task.id],
                "unassigned_task_ids": [],
                "compiled_cluster_assignments": [
                    {
                        "cluster_id": "cluster_1",
                        "task_ids_in_execution_order": [recovery_task.id],
                    }
                ],
            },
        ),
    )

    process_batch_after_execution(
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
    _assert_trace_payload_uses_canonical_contract(payload)

    assert payload["patched_plan_version"] == 40
    assert payload["assigned_task_ids"] == [recovery_task.id]
    assert payload["unassigned_task_ids"] == []
    assert payload["source_run_ids_with_recovery"] == [failed_run.id]
    assert payload["preexisting_pending_valid_task_count"] == 1
    assert payload["new_recovery_pending_task_count"] == 1


def test_post_batch_completed_with_evaluation_has_no_blocking_flags(
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
        status="completed",
    )

    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Task completed successfully.",
    )

    make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="validation_result",
        content='{"decision":"completed"}',
    )

    plan = make_execution_plan(
        plan_version=80,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The plan can continue normally.",
        decision_signals=["remaining_plan_still_valid"],
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
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
    assert result.can_continue_after_application is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
    assert result.requires_manual_review is False


def test_post_batch_project_stage_closed_has_no_blocking_flags(
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
        title="Final completed task",
        status="completed",
    )

    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Final task completed successfully.",
    )

    make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="validation_result",
        content='{"decision":"completed"}',
    )

    plan = make_execution_plan(
        plan_version=81,
        batches=[
            {
                "batch_id": "batch_final",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_completed",
        project_stage_closed=True,
        stage_goals_satisfied=True,
        recommended_next_action="close_stage",
        recommended_next_action_reason="The stage goals are fully satisfied.",
        decision_signals=["stage_goals_satisfied", "close_stage"],
        remaining_plan_still_valid=True,
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
        batch_id="batch_final",
        persist_result=False,
    )

    assert result.status == "project_stage_closed"
    assert result.can_continue_after_application is False
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
    assert result.requires_manual_review is False


def test_post_batch_finalization_guard_blocked_requires_only_manual_review(
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
        title="Final task requiring repeated resequencing",
        status="completed",
    )

    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Final task completed but triggered finalization reopening.",
    )

    make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="validation_result",
        content='{"decision":"completed"}',
    )

    plan = make_execution_plan(
        plan_version=82,
        batches=[
            {
                "batch_id": "batch_final",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="resequence_remaining_batches",
        recommended_next_action_reason="Finalization must be reopened again.",
        decision_signals=["remaining_plan_still_valid", "followup_tasks_created"],
        plan_change_scope="local_resequencing",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=True,
        followup_atomic_tasks_required=True,
        followup_atomic_tasks_reason="Finalization requires one more local follow-up pass before closure.",
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="resequence_deferred",
            patched_execution_plan=None,
            requires_replan=False,
            notes=["Deferred resequence at finalization."],
            metadata={},
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_final",
        persist_result=False,
        finalization_iteration_count=2,
        max_finalization_iterations=2,
    )

    assert result.status == "finalization_guard_blocked"
    assert result.can_continue_after_application is False
    assert result.requires_manual_review is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"


def test_post_batch_finalization_reopened_never_continues_execution(
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
        title="Final task reopening finalization",
        status="completed",
    )

    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Final task completed but further finalization work is needed.",
    )

    make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="validation_result",
        content='{"decision":"completed"}',
    )

    plan = make_execution_plan(
        plan_version=83,
        batches=[
            {
                "batch_id": "batch_final",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="resequence_remaining_batches",
        recommended_next_action_reason="Finalization needs one more pass.",
        decision_signals=["remaining_plan_still_valid", "followup_tasks_created"],
        plan_change_scope="local_resequencing",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=True,
        followup_atomic_tasks_required=True,
        followup_atomic_tasks_reason="Finalization requires one more local follow-up pass before closure.",
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="resequence_deferred",
            patched_execution_plan=None,
            requires_replan=False,
            notes=["Deferred resequence at finalization."],
            metadata={},
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_final",
        persist_result=False,
        finalization_iteration_count=0,
        max_finalization_iterations=2,
    )

    assert result.status == "finalization_reopened"
    assert result.can_continue_after_application is False


def test_post_batch_finalization_reopened_has_consistent_flags(
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
        title="Final task reopening finalization",
        status="completed",
    )

    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Final task completed but requires another finalization pass.",
    )

    make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="validation_result",
        content='{"decision":"completed"}',
    )

    plan = make_execution_plan(
        plan_version=84,
        batches=[
            {
                "batch_id": "batch_final",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="resequence_remaining_batches",
        recommended_next_action_reason="One more finalization pass is needed.",
        decision_signals=["remaining_plan_still_valid", "followup_tasks_created"],
        plan_change_scope="local_resequencing",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=True,
        followup_atomic_tasks_required=True,
        followup_atomic_tasks_reason="Finalization requires one more local follow-up pass before closure.",
    )

    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: evaluation_output,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_evaluation_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.mutate_live_plan",
        lambda **kwargs: types.SimpleNamespace(
            mutation_kind="resequence_deferred",
            patched_execution_plan=None,
            requires_replan=False,
            notes=["Deferred resequence during finalization."],
            metadata={},
        ),
    )

    result = process_batch_after_execution(
        db_session,
        project_id=project.id,
        plan=plan,
        batch_id="batch_final",
        persist_result=False,
        finalization_iteration_count=0,
        max_finalization_iterations=2,
    )

    assert result.status == "finalization_reopened"
    assert result.can_continue_after_application is False
    assert result.requires_manual_review is False
    assert result.finalization_guard_triggered is False
    assert result.finalization_iteration_count == 1


def test_post_batch_project_stage_closed_has_consistent_flags(
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
        title="Final completed task",
        status="completed",
    )

    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Final task completed successfully.",
    )

    make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="validation_result",
        content='{"decision":"completed"}',
    )

    plan = make_execution_plan(
        plan_version=85,
        batches=[
            {
                "batch_id": "batch_final",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_completed",
        project_stage_closed=True,
        stage_goals_satisfied=True,
        recommended_next_action="close_stage",
        recommended_next_action_reason="The stage goals are fully satisfied.",
        decision_signals=["stage_goals_satisfied", "close_stage"],
        remaining_plan_still_valid=True,
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
        batch_id="batch_final",
        persist_result=False,
    )

    assert result.status == "project_stage_closed"
    assert result.can_continue_after_application is False
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
    assert result.requires_manual_review is False
    assert result.finalization_guard_triggered is False


def test_post_batch_completed_with_evaluation_has_consistent_flags(
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
        status="completed",
    )

    make_execution_run(
        task_id=task.id,
        status="succeeded",
        work_summary="Task completed successfully.",
    )

    make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="validation_result",
        content='{"decision":"completed"}',
    )

    plan = make_execution_plan(
        plan_version=86,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task.id],
                "evaluation_focus": ["functional_coverage"],
            }
        ],
    )

    evaluation_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        project_stage_closed=False,
        stage_goals_satisfied=False,
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The remaining plan is still valid and execution can continue.",
        decision_signals=["remaining_plan_still_valid"],
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
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
    assert result.can_continue_after_application is True
    assert result.resolved_intent_type != "replan"
    assert result.resolved_intent_type != "resequence"
    assert result.requires_manual_review is False
    assert result.finalization_guard_triggered is False


def test_build_validation_context_summary_for_recovery_includes_partial_gap_signals():
    task = type(
        "TaskStub",
        (),
        {
            "id": 101,
            "status": TASK_STATUS_PARTIAL,
        },
    )()

    validation_artifact = Artifact(
        id=501,
        task_id=101,
        artifact_type="validation_result",
        content=json.dumps(
            {
                "execution_run_id": 9001,
                "validator_key": "standard_task_validator",
                "discipline": "standard",
                "validation_mode": "post_execution",
                "decision": "partial",
                "summary": "The task achieved useful progress but still has a remaining gap.",
                "validated_scope": "Parser updated and unit tests adjusted.",
                "missing_scope": "Add integration coverage for the new parser branch.",
                "blockers": [],
                "manual_review_required": False,
                "followup_validation_required": True,
                "final_task_status": "partial",
            },
            ensure_ascii=False,
        ),
        created_by="test",
    )

    summary = _build_validation_context_summary(
        task=task,
        validation_artifact=validation_artifact,
    )
    parsed = json.loads(summary)

    assert parsed["task_id"] == 101
    assert parsed["task_status"] == TASK_STATUS_PARTIAL

    recovery_summary = parsed["validation_summary_for_recovery"]
    assert recovery_summary["artifact_id"] == 501
    assert recovery_summary["artifact_type"] == "validation_result"
    assert recovery_summary["execution_run_id"] == 9001
    assert recovery_summary["validator_key"] == "standard_task_validator"
    assert recovery_summary["discipline"] == "standard"
    assert recovery_summary["validation_mode"] == "post_execution"
    assert recovery_summary["decision"] == "partial"
    assert recovery_summary["summary"] == (
        "The task achieved useful progress but still has a remaining gap."
    )
    assert recovery_summary["validated_scope"] == (
        "Parser updated and unit tests adjusted."
    )
    assert recovery_summary["missing_scope"] == (
        "Add integration coverage for the new parser branch."
    )
    assert recovery_summary["blockers"] == []
    assert recovery_summary["manual_review_required"] is False
    assert recovery_summary["followup_validation_required"] is True
    assert recovery_summary["final_task_status"] == "partial"


def test_build_validation_context_summary_for_recovery_preserves_manual_review_signal():
    task = type(
        "TaskStub",
        (),
        {
            "id": 202,
            "status": TASK_STATUS_FAILED,
        },
    )()

    validation_artifact = Artifact(
        id=502,
        task_id=202,
        artifact_type="validation_result",
        content=json.dumps(
            {
                "execution_run_id": 9002,
                "validator_key": "standard_task_validator",
                "discipline": "standard",
                "validation_mode": "post_execution",
                "decision": "manual_review",
                "summary": "Automation could not safely determine whether the remaining ambiguity is acceptable.",
                "validated_scope": None,
                "missing_scope": "Clarify whether the generated migration matches the expected production schema.",
                "blockers": [
                    "Schema expectations are ambiguous.",
                    "Automated evidence is insufficient for safe completion.",
                ],
                "manual_review_required": True,
                "followup_validation_required": False,
                "final_task_status": "failed",
            },
            ensure_ascii=False,
        ),
        created_by="test",
    )

    summary = _build_validation_context_summary(
        task=task,
        validation_artifact=validation_artifact,
    )
    parsed = json.loads(summary)

    assert parsed["task_id"] == 202
    assert parsed["task_status"] == TASK_STATUS_FAILED

    recovery_summary = parsed["validation_summary_for_recovery"]
    assert recovery_summary["decision"] == "manual_review"
    assert recovery_summary["summary"] == (
        "Automation could not safely determine whether the remaining ambiguity is acceptable."
    )
    assert recovery_summary["validated_scope"] is None
    assert recovery_summary["missing_scope"] == (
        "Clarify whether the generated migration matches the expected production schema."
    )
    assert recovery_summary["blockers"] == [
        "Schema expectations are ambiguous.",
        "Automated evidence is insufficient for safe completion.",
    ]
    assert recovery_summary["manual_review_required"] is True
    assert recovery_summary["followup_validation_required"] is False
    assert recovery_summary["final_task_status"] == "failed"


def test_build_validation_context_summary_for_recovery_handles_malformed_validation_artifact():
    task = type(
        "TaskStub",
        (),
        {
            "id": 303,
            "status": TASK_STATUS_FAILED,
        },
    )()

    validation_artifact = Artifact(
        id=503,
        task_id=303,
        artifact_type="validation_result",
        content="{this is not valid json",
        created_by="test",
    )

    summary = _build_validation_context_summary(
        task=task,
        validation_artifact=validation_artifact,
    )
    parsed = json.loads(summary)

    assert parsed["task_id"] == 303
    assert parsed["task_status"] == TASK_STATUS_FAILED

    recovery_summary = parsed["validation_summary_for_recovery"]
    assert recovery_summary["artifact_id"] == 503
    assert recovery_summary["artifact_type"] == "validation_result"
    assert recovery_summary["parse_error"] == (
        "validation artifact content is not valid JSON"
    )
    assert (
        recovery_summary["raw_validation_artifact_content"] == "{this is not valid json"
    )

    assert recovery_summary["execution_run_id"] is None
    assert recovery_summary["validator_key"] is None
    assert recovery_summary["discipline"] is None
    assert recovery_summary["validation_mode"] is None
    assert recovery_summary["decision"] is None
    assert recovery_summary["summary"] is None
    assert recovery_summary["validated_scope"] is None
    assert recovery_summary["missing_scope"] is None
    assert recovery_summary["blockers"] == []
    assert recovery_summary["manual_review_required"] is False
    assert recovery_summary["followup_validation_required"] is False
    assert recovery_summary["final_task_status"] is None
