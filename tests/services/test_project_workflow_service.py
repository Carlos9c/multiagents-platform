import types

import pytest

from app.models.task import PLANNING_LEVEL_HIGH_LEVEL, TASK_STATUS_PENDING
from app.services.project_workflow_service import (
    ProjectWorkflowServiceError,
    run_project_workflow,
)


def test_workflow_continues_to_next_batch_when_intermediate_checkpoint_is_stage_incomplete(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    task_1 = make_task(
        project_id=project.id,
        title="Atomic task 1",
        status=TASK_STATUS_PENDING,
    )
    task_2 = make_task(
        project_id=project.id,
        title="Atomic task 2",
        status=TASK_STATUS_PENDING,
    )
    task_3 = make_task(
        project_id=project.id,
        title="Atomic task 3",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id, task_2.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_3.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ]
    )

    monkeypatch.setattr(
        "app.services.project_workflow_service._bootstrap_project_storage_for_execution",
        lambda project_id: None,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_planner_if_needed",
        lambda db, project_id: True,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_optional_technical_refinement_phase",
        lambda db, project_id, enable_technical_refinement: True,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_atomic_generation_phase",
        lambda db, project_id, enable_technical_refinement: True,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.generate_execution_plan",
        lambda db, project_id: plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    def _fake_execute_task_sync(db, task_id):
        return types.SimpleNamespace(
            final_task_status="completed",
            validation_decision="completed",
        )

    monkeypatch.setattr(
        "app.services.project_workflow_service.execute_task_sync",
        _fake_execute_task_sync,
    )

    def _fake_post_batch(*, db, project_id, plan, batch_id, current_finalization_iteration_count, max_finalization_iterations):
        if batch_id == "batch_1":
            return types.SimpleNamespace(
                status="completed_with_evaluation",
                continue_execution=True,
                requires_manual_review=False,
                finalization_guard_triggered=False,
                requires_replanning=False,
                requires_resequencing=False,
                finalization_iteration_count=current_finalization_iteration_count,
            )

        return types.SimpleNamespace(
            status="project_stage_closed",
            continue_execution=False,
            requires_manual_review=False,
            finalization_guard_triggered=False,
            requires_replanning=False,
            requires_resequencing=False,
            finalization_iteration_count=current_finalization_iteration_count,
        )

    monkeypatch.setattr(
        "app.services.project_workflow_service._process_batch_after_terminal_tasks",
        _fake_post_batch,
    )

    result = run_project_workflow(
        db=db_session,
        project_id=project.id,
        max_workflow_iterations=2,
        max_finalization_iterations=2,
        enable_technical_refinement=False,
    )

    assert result.final_stage_closed is True
    assert result.status == "stage_closed"
    assert result.completed_batches == ["batch_1", "batch_2"]
    assert result.iterations[0].batch_ids_processed == ["batch_1", "batch_2"]


def test_workflow_rejects_non_atomic_task_inside_execution_batch(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()
    non_atomic_task = make_task(
        project_id=project.id,
        title="High-level task incorrectly inserted into batch",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        status=TASK_STATUS_PENDING,
        executor_type="pending_atomic_assignment",
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [non_atomic_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ]
    )

    monkeypatch.setattr(
        "app.services.project_workflow_service._bootstrap_project_storage_for_execution",
        lambda project_id: None,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_planner_if_needed",
        lambda db, project_id: True,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_optional_technical_refinement_phase",
        lambda db, project_id, enable_technical_refinement: True,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_atomic_generation_phase",
        lambda db, project_id, enable_technical_refinement: True,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.generate_execution_plan",
        lambda db, project_id: plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    def _should_not_execute(db, task_id):
        raise AssertionError("execute_task_sync should never be called for a non-atomic task.")

    monkeypatch.setattr(
        "app.services.project_workflow_service.execute_task_sync",
        _should_not_execute,
    )

    with pytest.raises(ProjectWorkflowServiceError, match="Only atomic tasks may be executed"):
        run_project_workflow(
            db=db_session,
            project_id=project.id,
            max_workflow_iterations=1,
            max_finalization_iterations=1,
            enable_technical_refinement=False,
        )