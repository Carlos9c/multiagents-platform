# tests/services/test_post_batch_service_problematic_outcomes.py

import json

import pytest

from app.models.task import (
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
)
from app.services.post_batch_service import PostBatchServiceError, process_batch_after_execution


def test_post_batch_allows_failed_task_without_validation_artifact_when_execution_failed_before_validation(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_execution_plan,
    make_recovery_decision,
    make_stage_evaluation_output,
):
    project = make_project()

    failed_task = make_task(
        project_id=project.id,
        title="Failed task without validation",
        status=TASK_STATUS_FAILED,
    )
    run = make_execution_run(
        task_id=failed_task.id,
        status="failed",
        failure_type="internal",
        failure_code="executor_failed",
        work_summary="Executor failed before validation could start.",
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
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [pending_followup.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ]
    )

    captured = {}

    def _fake_generate_recovery_decision(**kwargs):
        captured.update(kwargs)
        return make_recovery_decision(
            source_task_id=failed_task.id,
            source_run_id=run.id,
            action="manual_review",
            requires_manual_review=True,
            still_blocks_progress=True,
            created_tasks=[],
            reason="Execution failed before validation.",
            covered_gap_summary="Recovery must inspect the execution failure directly.",
        )

    monkeypatch.setattr(
        "app.services.post_batch_service.generate_recovery_decision",
        _fake_generate_recovery_decision,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.persist_recovery_decision",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.materialize_recovery_decision",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "app.services.post_batch_service.evaluate_checkpoint",
        lambda **kwargs: make_stage_evaluation_output(
            decision="stage_incomplete",
            project_stage_closed=False,
            stage_goals_satisfied=False,
            recommended_next_action="continue_current_plan",
            recommended_next_action_reason="The remaining plan is still valid.",
            decision_signals=["remaining_plan_still_valid"],
            notes=["Continue with the next batch."],
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

    validation_summary = json.loads(captured["validation_context_summary"])
    execution_summary = json.loads(captured["execution_context_summary"])

    assert result.problematic_run_ids == [run.id]
    assert validation_summary["validation_available"] is False
    assert validation_summary["recovery_posture"] == "execution_failed_before_validation"
    assert validation_summary["execution_failure_context"]["run_id"] == run.id
    assert execution_summary["latest_run"]["run_status"] == "failed"


def test_post_batch_rejects_partial_task_without_validation_artifact(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_execution_plan,
):
    project = make_project()

    partial_task = make_task(
        project_id=project.id,
        title="Partial task without validation",
        status=TASK_STATUS_PARTIAL,
    )
    make_execution_run(
        task_id=partial_task.id,
        status="partial",
        work_summary="Execution produced a partial result.",
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
                "task_ids": [partial_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [pending_followup.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ]
    )

    with pytest.raises(
        PostBatchServiceError,
        match=r"Partial tasks must always come from a completed validation flow",
    ):
        process_batch_after_execution(
            db_session,
            project_id=project.id,
            plan=plan,
            batch_id="batch_1",
            persist_result=False,
        )


def test_post_batch_rejects_failed_task_without_validation_artifact_when_latest_run_did_not_fail(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_execution_plan,
):
    project = make_project()

    failed_task = make_task(
        project_id=project.id,
        title="Failed task with inconsistent run outcome",
        status=TASK_STATUS_FAILED,
    )
    make_execution_run(
        task_id=failed_task.id,
        status="succeeded",
        work_summary="Execution succeeded but task was marked failed later.",
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
                "task_ids": [failed_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [pending_followup.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ]
    )

    with pytest.raises(
        PostBatchServiceError,
        match=r"A failed task without validation is only valid when execution terminated before validation",
    ):
        process_batch_after_execution(
            db_session,
            project_id=project.id,
            plan=plan,
            batch_id="batch_1",
            persist_result=False,
        )
