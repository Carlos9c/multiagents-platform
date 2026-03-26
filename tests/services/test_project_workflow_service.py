import types

import pytest

from app.models.project import Project
from app.models.task import (
    EXECUTION_ENGINE,
    PENDING_ENGINE_ROUTING_EXECUTOR,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_PENDING,
)
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
    project = make_project(
        name="Proyecto workflow",
        description="Proyecto de prueba",
    )
    project.enable_technical_refinement = False
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

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
        lambda db, project_id, *, enable_technical_refinement: True,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_atomic_generation_phase",
        lambda db, project_id, *, enable_technical_refinement: True,
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
            executor_type=EXECUTION_ENGINE,
        )

    monkeypatch.setattr(
        "app.services.project_workflow_service.execute_task_sync",
        _fake_execute_task_sync,
    )

    def _fake_post_batch(
        db,
        project_id,
        plan,
        batch_id,
        current_finalization_iteration_count,
        max_finalization_iterations,
        checkpoint_artifact_window_start_exclusive,
    ):
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
    project = make_project(
        name="Proyecto workflow",
        description="Proyecto de prueba",
    )
    project.enable_technical_refinement = False
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    non_atomic_task = make_task(
        project_id=project.id,
        title="High-level task incorrectly inserted into batch",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        status=TASK_STATUS_PENDING,
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
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
        lambda db, project_id, *, enable_technical_refinement: True,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_atomic_generation_phase",
        lambda db, project_id, *, enable_technical_refinement: True,
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
        )


def test_workflow_uses_project_enable_technical_refinement_flag(
    db_session,
    monkeypatch,
    make_project,
):
    project = make_project(
        name="Proyecto con refinement",
        description="Proyecto de prueba",
    )
    project.enable_technical_refinement = True
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    captured = {
        "refinement_flag_in_refiner_phase": None,
        "refinement_flag_in_atomic_phase": None,
    }

    monkeypatch.setattr(
        "app.services.project_workflow_service._bootstrap_project_storage_for_execution",
        lambda project_id: None,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_planner_if_needed",
        lambda db, project_id: True,
    )

    def _fake_run_optional_technical_refinement_phase(
        db,
        project_id,
        *,
        enable_technical_refinement,
    ):
        captured["refinement_flag_in_refiner_phase"] = enable_technical_refinement
        return True

    def _fake_run_atomic_generation_phase(
        db,
        project_id,
        *,
        enable_technical_refinement,
    ):
        captured["refinement_flag_in_atomic_phase"] = enable_technical_refinement
        return True

    monkeypatch.setattr(
        "app.services.project_workflow_service._run_optional_technical_refinement_phase",
        _fake_run_optional_technical_refinement_phase,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_atomic_generation_phase",
        _fake_run_atomic_generation_phase,
    )

    monkeypatch.setattr(
        "app.services.project_workflow_service.generate_execution_plan",
        lambda db, project_id: types.SimpleNamespace(
            plan_version=1,
            execution_batches=[],
        ),
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    result = run_project_workflow(
        db=db_session,
        project_id=project.id,
        max_workflow_iterations=1,
        max_finalization_iterations=1,
    )

    assert captured["refinement_flag_in_refiner_phase"] is True
    assert captured["refinement_flag_in_atomic_phase"] is True
    assert result.refinement_completed is True
    assert result.atomic_generation_completed is True


def test_workflow_bypasses_refinement_when_project_flag_is_false(
    db_session,
    monkeypatch,
    make_project,
):
    project = make_project(
        name="Proyecto sin refinement",
        description="Proyecto de prueba",
    )
    project.enable_technical_refinement = False
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    captured = {
        "refinement_flag_in_refiner_phase": None,
        "refinement_flag_in_atomic_phase": None,
    }

    monkeypatch.setattr(
        "app.services.project_workflow_service._bootstrap_project_storage_for_execution",
        lambda project_id: None,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_planner_if_needed",
        lambda db, project_id: True,
    )

    def _fake_run_optional_technical_refinement_phase(
        db,
        project_id,
        *,
        enable_technical_refinement,
    ):
        captured["refinement_flag_in_refiner_phase"] = enable_technical_refinement
        return True

    def _fake_run_atomic_generation_phase(
        db,
        project_id,
        *,
        enable_technical_refinement,
    ):
        captured["refinement_flag_in_atomic_phase"] = enable_technical_refinement
        return True

    monkeypatch.setattr(
        "app.services.project_workflow_service._run_optional_technical_refinement_phase",
        _fake_run_optional_technical_refinement_phase,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service._run_atomic_generation_phase",
        _fake_run_atomic_generation_phase,
    )

    monkeypatch.setattr(
        "app.services.project_workflow_service.generate_execution_plan",
        lambda db, project_id: types.SimpleNamespace(
            plan_version=1,
            execution_batches=[],
        ),
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    result = run_project_workflow(
        db=db_session,
        project_id=project.id,
        max_workflow_iterations=1,
        max_finalization_iterations=1,
    )

    assert captured["refinement_flag_in_refiner_phase"] is False
    assert captured["refinement_flag_in_atomic_phase"] is False
    assert result.refinement_completed is True
    assert result.atomic_generation_completed is True

def test_workflow_trace_keeps_batch_identity_stable_across_plan_versions(
    db_session,
    make_project,
    make_execution_plan,
):
    project = make_project(plan_version=1)

    plan_v1 = make_execution_plan(
        plan_version=1,
        supersedes_plan_version=None,
        batches=[
            {"task_ids": [1]},
        ],
    )
    plan_v2 = make_execution_plan(
        plan_version=2,
        supersedes_plan_version=1,
        batches=[
            {"task_ids": [2]},
        ],
    )

    batch_v1 = plan_v1.execution_batches[0]
    batch_v2 = plan_v2.execution_batches[0]

    assert batch_v1.batch_index == 1
    assert batch_v2.batch_index == 1
    assert batch_v1.batch_internal_id != batch_v2.batch_internal_id
    assert batch_v1.plan_version == 1
    assert batch_v2.plan_version == 2