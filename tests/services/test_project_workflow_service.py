import types
import json

from app.models.artifact import Artifact
import pytest

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


def _post_batch_result(
    *,
    status: str,
    resolved_intent_type: str,
    resolved_mutation_scope: str = "none",
    remaining_plan_still_valid: bool = True,
    has_new_recovery_tasks: bool = False,
    requires_plan_mutation: bool = False,
    requires_all_new_tasks_assigned: bool = False,
    can_continue_after_application: bool = False,
    should_close_stage: bool = False,
    requires_manual_review: bool = False,
    reopened_finalization: bool = False,
    patched_execution_plan=None,
    decision_signals: list[str] | None = None,
    finalization_iteration_count: int = 0,
    finalization_guard_triggered: bool = False,
    notes: str = "post batch result",
):
    return types.SimpleNamespace(
        status=status,
        resolved_intent_type=resolved_intent_type,
        resolved_mutation_scope=resolved_mutation_scope,
        remaining_plan_still_valid=remaining_plan_still_valid,
        has_new_recovery_tasks=has_new_recovery_tasks,
        requires_plan_mutation=requires_plan_mutation,
        requires_all_new_tasks_assigned=requires_all_new_tasks_assigned,
        can_continue_after_application=can_continue_after_application,
        should_close_stage=should_close_stage,
        requires_manual_review=requires_manual_review,
        reopened_finalization=reopened_finalization,
        patched_execution_plan=patched_execution_plan,
        decision_signals=decision_signals or [],
        finalization_iteration_count=finalization_iteration_count,
        finalization_guard_triggered=finalization_guard_triggered,
        notes=notes,
    )


def _empty_execution_plan(*, plan_version: int = 1):
    return types.SimpleNamespace(
        plan_version=plan_version,
        execution_batches=[],
        checkpoints=[],
    )


def _successful_execution_result(*, executor_type: str = EXECUTION_ENGINE):
    return types.SimpleNamespace(
        final_task_status="completed",
        validation_decision="completed",
        executor_type=executor_type,
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
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

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
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="continue",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        return _post_batch_result(
            status="project_stage_closed",
            resolved_intent_type="close",
            resolved_mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=False,
            should_close_stage=True,
            requires_manual_review=False,
            reopened_finalization=False,
            patched_execution_plan=None,
            decision_signals=[],
            finalization_iteration_count=current_finalization_iteration_count,
            finalization_guard_triggered=False,
            notes="post batch result",
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
        lambda db, project_id: _empty_execution_plan(plan_version=1),
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
        lambda db, project_id: _empty_execution_plan(plan_version=1),
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
    make_project(plan_version=1)

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


def test_workflow_adopts_patched_execution_plan_even_when_assignment_does_not_require_resequence(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Proyecto workflow con assignment",
        description="Proyecto de prueba",
    )
    project.enable_technical_refinement = False
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    batch_1_task = make_task(
        project_id=project.id,
        title="Atomic task 1",
        status=TASK_STATUS_PENDING,
    )
    inserted_patch_task = make_task(
        project_id=project.id,
        title="Assigned recovery patch task",
        status=TASK_STATUS_PENDING,
    )
    final_batch_task = make_task(
        project_id=project.id,
        title="Atomic task 2",
        status=TASK_STATUS_PENDING,
    )

    original_plan = make_execution_plan(
        plan_version=4,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [batch_1_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [final_batch_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    patched_plan = make_execution_plan(
        plan_version=4,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [batch_1_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_1_patch_1",
                "batch_internal_id": "4_1_p1",
                "batch_index": 1,
                "plan_version": 4,
                "task_ids": [inserted_patch_task.id],
                "checkpoint_id": "checkpoint_batch_1_patch_1",
                "checkpoint_name": "Patch checkpoint 1.1",
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [final_batch_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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
        lambda db, project_id: original_plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    executed_task_ids = []

    def _fake_execute_task_sync(db, task_id):
        executed_task_ids.append(task_id)
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

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
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="assign",
                resolved_mutation_scope="assignment",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=True,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=True,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=patched_plan,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        if batch_id == "batch_1_patch_1":
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="continue",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        return _post_batch_result(
            status="project_stage_closed",
            resolved_intent_type="close",
            resolved_mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=False,
            should_close_stage=True,
            requires_manual_review=False,
            reopened_finalization=False,
            patched_execution_plan=None,
            decision_signals=[],
            finalization_iteration_count=current_finalization_iteration_count,
            finalization_guard_triggered=False,
            notes="post batch result",
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
    assert result.completed_batches == ["batch_1", "batch_1_patch_1", "batch_2"]
    assert result.iterations[0].batch_ids_processed == [
        "batch_1",
        "batch_1_patch_1",
        "batch_2",
    ]
    assert executed_task_ids == [
        batch_1_task.id,
        inserted_patch_task.id,
        final_batch_task.id,
    ]


def test_workflow_invalidates_active_plan_when_iteration_requires_replan(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow replan test",
        description="Project for workflow replan regression",
    )
    project.enable_technical_refinement = False
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    batch_1_task = make_task(
        project_id=project.id,
        title="Atomic task 1",
        status=TASK_STATUS_PENDING,
    )
    replanned_task = make_task(
        project_id=project.id,
        title="Replanned atomic task",
        status=TASK_STATUS_PENDING,
    )

    original_plan = make_execution_plan(
        plan_version=5,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [batch_1_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    replanned_plan = make_execution_plan(
        plan_version=6,
        supersedes_plan_version=5,
        batches=[
            {
                "batch_id": "batch_replanned",
                "task_ids": [replanned_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    generated_plan_versions = []

    def _fake_generate_execution_plan(db, project_id):
        if not generated_plan_versions:
            generated_plan_versions.append(5)
            return original_plan
        generated_plan_versions.append(6)
        return replanned_plan

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
        _fake_generate_execution_plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    executed_task_ids = []

    def _fake_execute_task_sync(db, task_id):
        executed_task_ids.append(task_id)
        return _successful_execution_result(executor_type="execution_engine")

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
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="replan",
                resolved_mutation_scope="replan",
                remaining_plan_still_valid=False,
                has_new_recovery_tasks=False,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=True,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        return _post_batch_result(
            status="project_stage_closed",
            resolved_intent_type="close",
            resolved_mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=False,
            should_close_stage=True,
            requires_manual_review=False,
            reopened_finalization=False,
            patched_execution_plan=None,
            decision_signals=[],
            finalization_iteration_count=current_finalization_iteration_count,
            finalization_guard_triggered=False,
            notes="post batch result",
        )

    monkeypatch.setattr(
        "app.services.project_workflow_service._process_batch_after_terminal_tasks",
        _fake_post_batch,
    )

    result = run_project_workflow(
        db=db_session,
        project_id=project.id,
        max_workflow_iterations=3,
        max_finalization_iterations=2,
    )

    assert result.final_stage_closed is True
    assert result.status == "stage_closed"
    assert generated_plan_versions == [5, 6]
    assert executed_task_ids == [batch_1_task.id, replanned_task.id]
    assert result.completed_batches == ["batch_1", "batch_replanned"]


def test_workflow_reuses_active_plan_after_assignment_patch(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow active plan reuse",
        description="Project for active plan reuse regression",
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
    patch_task = make_task(
        project_id=project.id,
        title="Patched task",
        status=TASK_STATUS_PENDING,
    )
    task_2 = make_task(
        project_id=project.id,
        title="Atomic task 2",
        status=TASK_STATUS_PENDING,
    )

    original_plan = make_execution_plan(
        plan_version=7,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    patched_plan = make_execution_plan(
        plan_version=7,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_1_patch_1",
                "batch_internal_id": "7_1_p1",
                "batch_index": 1,
                "plan_version": 7,
                "task_ids": [patch_task.id],
                "checkpoint_id": "checkpoint_batch_1_patch_1",
                "checkpoint_name": "Patch checkpoint 1.1",
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    generate_calls = []

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
        lambda db, project_id: generate_calls.append("generate") or original_plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    executed_task_ids = []

    def _fake_execute_task_sync(db, task_id):
        executed_task_ids.append(task_id)
        return _successful_execution_result(executor_type="execution_engine")

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
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="assign",
                resolved_mutation_scope="assignment",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=True,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=True,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=patched_plan,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        if batch_id == "batch_1_patch_1":
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="continue",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        return _post_batch_result(
            status="project_stage_closed",
            resolved_intent_type="close",
            resolved_mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=False,
            should_close_stage=True,
            requires_manual_review=False,
            reopened_finalization=False,
            patched_execution_plan=None,
            decision_signals=[],
            finalization_iteration_count=current_finalization_iteration_count,
            finalization_guard_triggered=False,
            notes="post batch result",
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
    assert generate_calls == ["generate"]
    assert executed_task_ids == [task_1.id, patch_task.id, task_2.id]
    assert result.completed_batches == ["batch_1", "batch_1_patch_1", "batch_2"]


def test_workflow_does_not_reexecute_completed_batch_after_deferred_resequence(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow deferred resequence reuse",
        description="Project for deferred resequence regression",
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

    plan = make_execution_plan(
        plan_version=8,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    generate_calls = []

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
        lambda db, project_id: generate_calls.append("generate") or plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    executed_task_ids = []

    def _fake_execute_task_sync(db, task_id):
        executed_task_ids.append(task_id)
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

    monkeypatch.setattr(
        "app.services.project_workflow_service.execute_task_sync",
        _fake_execute_task_sync,
    )

    post_batch_calls = {"batch_1": 0, "batch_2": 0}

    def _fake_post_batch(
        db,
        project_id,
        plan,
        batch_id,
        current_finalization_iteration_count,
        max_finalization_iterations,
        checkpoint_artifact_window_start_exclusive,
    ):
        post_batch_calls[batch_id] += 1

        if batch_id == "batch_1" and post_batch_calls[batch_id] == 1:
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="resequence",
                resolved_mutation_scope="resequence",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        if batch_id == "batch_2":
            return _post_batch_result(
                status="project_stage_closed",
                resolved_intent_type="close",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=True,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        return _post_batch_result(
            status="completed_with_evaluation",
            resolved_intent_type="continue",
            resolved_mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=True,
            should_close_stage=False,
            requires_manual_review=False,
            reopened_finalization=False,
            patched_execution_plan=None,
            decision_signals=[],
            finalization_iteration_count=current_finalization_iteration_count,
            finalization_guard_triggered=False,
            notes="post batch result",
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
    assert generate_calls == ["generate"]
    assert executed_task_ids == [task_1.id, task_2.id]
    assert result.completed_batches == ["batch_1", "batch_2"]
    assert result.iterations[0].batch_ids_processed == ["batch_1"]
    assert result.iterations[1].batch_ids_processed == ["batch_2"]


def test_workflow_keeps_completed_batches_unique_across_multiple_deferred_resequence_iterations(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow repeated deferred resequence",
        description="Project for repeated deferred resequence regression",
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
        plan_version=9,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_3",
                "task_ids": [task_3.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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

    executed_task_ids = []

    def _fake_execute_task_sync(db, task_id):
        executed_task_ids.append(task_id)
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

    monkeypatch.setattr(
        "app.services.project_workflow_service.execute_task_sync",
        _fake_execute_task_sync,
    )

    post_batch_calls = {"batch_1": 0, "batch_2": 0, "batch_3": 0}

    def _fake_post_batch(
        db,
        project_id,
        plan,
        batch_id,
        current_finalization_iteration_count,
        max_finalization_iterations,
        checkpoint_artifact_window_start_exclusive,
    ):
        post_batch_calls[batch_id] += 1

        if batch_id in {"batch_1", "batch_2"} and post_batch_calls[batch_id] == 1:
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="resequence",
                resolved_mutation_scope="resequence",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        if batch_id == "batch_3":
            return _post_batch_result(
                status="project_stage_closed",
                resolved_intent_type="close",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=True,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        return _post_batch_result(
            status="completed_with_evaluation",
            resolved_intent_type="continue",
            resolved_mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=True,
            should_close_stage=False,
            requires_manual_review=False,
            reopened_finalization=False,
            patched_execution_plan=None,
            decision_signals=[],
            finalization_iteration_count=current_finalization_iteration_count,
            finalization_guard_triggered=False,
            notes="post batch result",
        )

    monkeypatch.setattr(
        "app.services.project_workflow_service._process_batch_after_terminal_tasks",
        _fake_post_batch,
    )

    result = run_project_workflow(
        db=db_session,
        project_id=project.id,
        max_workflow_iterations=3,
        max_finalization_iterations=2,
    )

    assert result.final_stage_closed is True
    assert result.status == "stage_closed"
    assert executed_task_ids == [task_1.id, task_2.id, task_3.id]
    assert result.completed_batches == ["batch_1", "batch_2", "batch_3"]
    assert [iteration.batch_ids_processed for iteration in result.iterations] == [
        ["batch_1"],
        ["batch_2"],
        ["batch_3"],
    ]


def test_workflow_reuses_patched_active_plan_without_regenerating_execution_plan(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow reuses patched active plan",
        description="Project for active_plan reuse after local mutation",
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
    patch_task = make_task(
        project_id=project.id,
        title="Patched atomic task",
        status=TASK_STATUS_PENDING,
    )
    task_2 = make_task(
        project_id=project.id,
        title="Atomic task 2",
        status=TASK_STATUS_PENDING,
    )

    initial_plan = make_execution_plan(
        plan_version=20,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    patched_plan = make_execution_plan(
        plan_version=20,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_1_patch_1",
                "task_ids": [patch_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    generate_calls = []

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
        lambda db, project_id: generate_calls.append("generate") or initial_plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    executed_task_ids = []

    def _fake_execute_task_sync(db, task_id):
        executed_task_ids.append(task_id)
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

    monkeypatch.setattr(
        "app.services.project_workflow_service.execute_task_sync",
        _fake_execute_task_sync,
    )

    post_batch_calls = {"batch_1": 0, "batch_1_patch_1": 0, "batch_2": 0}

    def _fake_post_batch(
        db,
        project_id,
        plan,
        batch_id,
        current_finalization_iteration_count,
        max_finalization_iterations,
        checkpoint_artifact_window_start_exclusive,
    ):
        post_batch_calls[batch_id] += 1

        if batch_id == "batch_1":
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="assign",
                resolved_mutation_scope="assignment",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=True,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=True,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=patched_plan,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        if batch_id == "batch_1_patch_1":
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="continue",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        return _post_batch_result(
            status="project_stage_closed",
            resolved_intent_type="close",
            resolved_mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=False,
            should_close_stage=True,
            requires_manual_review=False,
            reopened_finalization=False,
            patched_execution_plan=None,
            decision_signals=[],
            finalization_iteration_count=current_finalization_iteration_count,
            finalization_guard_triggered=False,
            notes="post batch result",
        )

    monkeypatch.setattr(
        "app.services.project_workflow_service._process_batch_after_terminal_tasks",
        _fake_post_batch,
    )

    result = run_project_workflow(
        db=db_session,
        project_id=project.id,
        max_workflow_iterations=3,
        max_finalization_iterations=2,
    )

    assert result.final_stage_closed is True
    assert result.status == "stage_closed"
    assert generate_calls == ["generate"]
    assert executed_task_ids == [task_1.id, patch_task.id, task_2.id]
    assert result.completed_batches == ["batch_1", "batch_1_patch_1", "batch_2"]


def test_workflow_resequence_does_not_regenerate_execution_plan_or_set_structural_replan(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow resequence without structural replan",
        description="Project for validating that resequencing stays local",
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

    plan = make_execution_plan(
        plan_version=21,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    generate_calls = []

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
        lambda db, project_id: generate_calls.append("generate") or plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    executed_task_ids = []

    def _fake_execute_task_sync(db, task_id):
        executed_task_ids.append(task_id)
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

    monkeypatch.setattr(
        "app.services.project_workflow_service.execute_task_sync",
        _fake_execute_task_sync,
    )

    post_batch_calls = {"batch_1": 0, "batch_2": 0}

    def _fake_post_batch(
        db,
        project_id,
        plan,
        batch_id,
        current_finalization_iteration_count,
        max_finalization_iterations,
        checkpoint_artifact_window_start_exclusive,
    ):
        post_batch_calls[batch_id] += 1

        if batch_id == "batch_1":
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="resequence",
                resolved_mutation_scope="resequence",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        return _post_batch_result(
            status="project_stage_closed",
            resolved_intent_type="close",
            resolved_mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=False,
            should_close_stage=True,
            requires_manual_review=False,
            reopened_finalization=False,
            patched_execution_plan=None,
            decision_signals=[],
            finalization_iteration_count=current_finalization_iteration_count,
            finalization_guard_triggered=False,
            notes="post batch result",
        )

    monkeypatch.setattr(
        "app.services.project_workflow_service._process_batch_after_terminal_tasks",
        _fake_post_batch,
    )

    result = run_project_workflow(
        db=db_session,
        project_id=project.id,
        max_workflow_iterations=3,
        max_finalization_iterations=2,
    )

    assert result.final_stage_closed is True
    assert result.status == "stage_closed"
    assert generate_calls == ["generate"]
    assert executed_task_ids == [task_1.id, task_2.id]
    assert result.completed_batches == ["batch_1", "batch_2"]
    assert [iteration.batch_ids_processed for iteration in result.iterations] == [
        ["batch_1"],
        ["batch_2"],
    ]


def test_workflow_blocked_batches_reflect_remaining_batches_from_latest_active_plan(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow blocked batches from latest active plan",
        description="Project for validating blocked_batches after local plan mutation",
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
    patch_task = make_task(
        project_id=project.id,
        title="Patched atomic task",
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

    initial_plan = make_execution_plan(
        plan_version=22,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_3",
                "task_ids": [task_3.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    patched_plan = make_execution_plan(
        plan_version=22,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_1_patch_1",
                "task_ids": [patch_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_3",
                "task_ids": [task_3.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    generate_calls = []

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
        lambda db, project_id: generate_calls.append("generate") or initial_plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    executed_task_ids = []

    def _fake_execute_task_sync(db, task_id):
        executed_task_ids.append(task_id)
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

    monkeypatch.setattr(
        "app.services.project_workflow_service.execute_task_sync",
        _fake_execute_task_sync,
    )

    post_batch_calls = {"batch_1": 0, "batch_1_patch_1": 0, "batch_2": 0, "batch_3": 0}

    def _fake_post_batch(
        db,
        project_id,
        plan,
        batch_id,
        current_finalization_iteration_count,
        max_finalization_iterations,
        checkpoint_artifact_window_start_exclusive,
    ):
        post_batch_calls[batch_id] += 1

        if batch_id == "batch_1":
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="assign",
                resolved_mutation_scope="assignment",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=True,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=True,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=patched_plan,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        if batch_id == "batch_1_patch_1":
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="continue",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        if batch_id == "batch_2":
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="manual_review",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=True,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        return _post_batch_result(
            status="completed_with_evaluation",
            resolved_intent_type="continue",
            resolved_mutation_scope="none",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=False,
            requires_plan_mutation=False,
            requires_all_new_tasks_assigned=False,
            can_continue_after_application=True,
            should_close_stage=False,
            requires_manual_review=False,
            reopened_finalization=False,
            patched_execution_plan=None,
            decision_signals=[],
            finalization_iteration_count=current_finalization_iteration_count,
            finalization_guard_triggered=False,
            notes="post batch result",
        )

    monkeypatch.setattr(
        "app.services.project_workflow_service._process_batch_after_terminal_tasks",
        _fake_post_batch,
    )

    result = run_project_workflow(
        db=db_session,
        project_id=project.id,
        max_workflow_iterations=3,
        max_finalization_iterations=2,
    )

    assert result.final_stage_closed is False
    assert result.manual_review_required is True
    assert result.status == "awaiting_manual_review"
    assert generate_calls == ["generate"]
    assert executed_task_ids == [task_1.id, patch_task.id, task_2.id]
    assert result.completed_batches == ["batch_1", "batch_1_patch_1", "batch_2"]
    assert result.blocked_batches == ["batch_3"]


def test_workflow_iteration_summary_tracks_plan_transition_and_blocked_batches(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow iteration trace of active plan transition",
        description="Project for validating iteration summary traceability",
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
    patch_task = make_task(
        project_id=project.id,
        title="Patched task",
        status=TASK_STATUS_PENDING,
    )
    task_2 = make_task(
        project_id=project.id,
        title="Atomic task 2",
        status=TASK_STATUS_PENDING,
    )

    initial_plan = make_execution_plan(
        plan_version=50,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    patched_plan = make_execution_plan(
        plan_version=50,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_1_patch_1",
                "task_ids": [patch_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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
        lambda db, project_id: initial_plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    def _fake_execute_task_sync(db, task_id):
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

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
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="assign",
                resolved_mutation_scope="assignment",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=True,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=True,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=patched_plan,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        if batch_id == "batch_1_patch_1":
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="manual_review",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=True,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="post batch result",
            )

        raise AssertionError(f"Unexpected batch_id {batch_id}")

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

    assert result.status == "awaiting_manual_review"
    assert result.manual_review_required is True
    assert len(result.iterations) == 1

    iteration = result.iterations[0]
    assert iteration.starting_plan_version == 50
    assert iteration.ending_plan_version == 50
    assert (
        iteration.used_patched_plan is True
    )  # se aplicó un patched plan aunque la plan_version no cambie
    assert (iteration.resolved_intent_type == "replan") is False
    assert iteration.batch_ids_processed == ["batch_1", "batch_1_patch_1"]
    assert iteration.blocked_batch_ids_after_iteration == ["batch_2"]


def test_workflow_batch_trace_includes_resolved_action_decision_signals_and_patched_plan_version(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow batch trace enrichment",
        description="Project for validating enriched workflow_batch_trace payload",
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
    patch_task = make_task(
        project_id=project.id,
        title="Patched task",
        status=TASK_STATUS_PENDING,
    )
    task_2 = make_task(
        project_id=project.id,
        title="Atomic task 2",
        status=TASK_STATUS_PENDING,
    )

    initial_plan = make_execution_plan(
        plan_version=60,
        batches=[
            {
                "batch_id": "batch_1",
                "batch_internal_id": "60_1",
                "batch_index": 1,
                "plan_version": 60,
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "batch_internal_id": "60_2",
                "batch_index": 2,
                "plan_version": 60,
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    patched_plan = make_execution_plan(
        plan_version=60,
        batches=[
            {
                "batch_id": "batch_1",
                "batch_internal_id": "60_1",
                "batch_index": 1,
                "plan_version": 60,
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_1_patch_1",
                "batch_internal_id": "60_1_p1",
                "batch_index": 1,
                "plan_version": 60,
                "task_ids": [patch_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "batch_internal_id": "60_2",
                "batch_index": 2,
                "plan_version": 60,
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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
        lambda db, project_id: initial_plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    def _fake_execute_task_sync(db, task_id):
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

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
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="assign",
                resolved_mutation_scope="assignment",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=True,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=True,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=patched_plan,
                decision_signals=[
                    "remaining_plan_still_valid",
                    "followup_tasks_created",
                ],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Recovery work was assigned into the live plan.",
            )

        if batch_id == "batch_1_patch_1":
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="manual_review",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=True,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["manual_review_required"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Manual review required after patch batch.",
            )

        raise AssertionError(f"Unexpected batch_id {batch_id}")

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

    assert result.status == "awaiting_manual_review"

    trace_artifacts = (
        db_session.query(Artifact)
        .filter(
            Artifact.project_id == project.id,
            Artifact.artifact_type == "workflow_batch_trace",
        )
        .order_by(Artifact.id.asc())
        .all()
    )

    assert len(trace_artifacts) == 2

    first_payload = json.loads(trace_artifacts[0].content)
    second_payload = json.loads(trace_artifacts[1].content)

    assert first_payload["batch_id"] == "batch_1"
    assert first_payload["resolved_intent_type"] == "assign"
    assert first_payload["decision_signals"] == [
        "remaining_plan_still_valid",
        "followup_tasks_created",
    ]
    assert first_payload["patched_plan_version"] == 60

    assert second_payload["batch_id"] == "batch_1_patch_1"
    assert second_payload["resolved_intent_type"] == "manual_review"
    assert second_payload["decision_signals"] == ["manual_review_required"]
    assert second_payload["patched_plan_version"] is None


def test_workflow_batch_trace_iteration_summary_and_result_blocked_batches_are_consistent(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow blocked batch consistency",
        description="Project for validating consistency across workflow trace outputs",
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
    patch_task = make_task(
        project_id=project.id,
        title="Patched task",
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

    initial_plan = make_execution_plan(
        plan_version=70,
        batches=[
            {
                "batch_id": "batch_1",
                "batch_internal_id": "70_1",
                "batch_index": 1,
                "plan_version": 70,
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "batch_internal_id": "70_2",
                "batch_index": 2,
                "plan_version": 70,
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_3",
                "batch_internal_id": "70_3",
                "batch_index": 3,
                "plan_version": 70,
                "task_ids": [task_3.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    patched_plan = make_execution_plan(
        plan_version=70,
        batches=[
            {
                "batch_id": "batch_1",
                "batch_internal_id": "70_1",
                "batch_index": 1,
                "plan_version": 70,
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_1_patch_1",
                "batch_internal_id": "70_1_p1",
                "batch_index": 1,
                "plan_version": 70,
                "task_ids": [patch_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "batch_internal_id": "70_2",
                "batch_index": 2,
                "plan_version": 70,
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_3",
                "batch_internal_id": "70_3",
                "batch_index": 3,
                "plan_version": 70,
                "task_ids": [task_3.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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
        lambda db, project_id: initial_plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    def _fake_execute_task_sync(db, task_id):
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

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
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="assign",
                resolved_mutation_scope="assignment",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=True,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=True,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=patched_plan,
                decision_signals=[
                    "remaining_plan_still_valid",
                    "followup_tasks_created",
                ],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Recovery work was assigned into the live plan.",
            )

        if batch_id == "batch_1_patch_1":
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="continue",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["patched_plan_continues"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Patch batch completed successfully.",
            )

        if batch_id == "batch_2":
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="manual_review",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=True,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["manual_review_required"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Manual review required before continuing to final batch.",
            )

        raise AssertionError(f"Unexpected batch_id {batch_id}")

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

    assert result.status == "awaiting_manual_review"
    assert result.manual_review_required is True
    assert result.blocked_batches == ["batch_3"]

    assert len(result.iterations) == 1
    iteration = result.iterations[0]
    assert iteration.batch_ids_processed == ["batch_1", "batch_1_patch_1", "batch_2"]
    assert iteration.blocked_batch_ids_after_iteration == ["batch_3"]

    trace_artifacts = (
        db_session.query(Artifact)
        .filter(
            Artifact.project_id == project.id,
            Artifact.artifact_type == "workflow_batch_trace",
        )
        .order_by(Artifact.id.asc())
        .all()
    )

    assert len(trace_artifacts) == 3

    last_trace_payload = json.loads(trace_artifacts[-1].content)
    assert last_trace_payload["batch_id"] == "batch_2"
    assert last_trace_payload["resolved_intent_type"] == "manual_review"

    assert iteration.blocked_batch_ids_after_iteration == result.blocked_batches == ["batch_3"]


def test_workflow_result_matches_last_iteration_when_manual_review_stops_execution(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow result consistency on manual review stop",
        description="Project for validating workflow-level status consistency",
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
        plan_version=90,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_3",
                "task_ids": [task_3.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

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
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="continue",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["remaining_plan_still_valid"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Continue to the next batch.",
            )

        if batch_id == "batch_2":
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="manual_review",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=True,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["manual_review_required"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Manual review required before final batch.",
            )

        raise AssertionError(f"Unexpected batch_id {batch_id}")

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

    assert result.status == "awaiting_manual_review"
    assert result.manual_review_required is True
    assert result.final_stage_closed is False
    assert result.blocked_batches == ["batch_3"]

    assert len(result.iterations) == 1
    last_iteration = result.iterations[-1]
    assert last_iteration.requires_manual_review is True
    assert last_iteration.blocked_batch_ids_after_iteration == ["batch_3"]


def test_workflow_artifacts_and_results_tell_a_consistent_execution_story(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow full story reconstruction",
        description="Project for validating consistency across workflow traces and result views",
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
    patch_task = make_task(
        project_id=project.id,
        title="Patch task",
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

    initial_plan = make_execution_plan(
        plan_version=100,
        batches=[
            {
                "batch_id": "batch_1",
                "batch_internal_id": "100_1",
                "batch_index": 1,
                "plan_version": 100,
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "batch_internal_id": "100_2",
                "batch_index": 2,
                "plan_version": 100,
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_3",
                "batch_internal_id": "100_3",
                "batch_index": 3,
                "plan_version": 100,
                "task_ids": [task_3.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    patched_plan = make_execution_plan(
        plan_version=100,
        batches=[
            {
                "batch_id": "batch_1",
                "batch_internal_id": "100_1",
                "batch_index": 1,
                "plan_version": 100,
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_1_patch_1",
                "batch_internal_id": "100_1_p1",
                "batch_index": 1,
                "plan_version": 100,
                "task_ids": [patch_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "batch_internal_id": "100_2",
                "batch_index": 2,
                "plan_version": 100,
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_3",
                "batch_internal_id": "100_3",
                "batch_index": 3,
                "plan_version": 100,
                "task_ids": [task_3.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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
        lambda db, project_id: initial_plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    def _fake_execute_task_sync(db, task_id):
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

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
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="assign",
                resolved_mutation_scope="assignment",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=True,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=True,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=patched_plan,
                decision_signals=[],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Recovery work was inserted into the live plan.",
            )

        if batch_id == "batch_1_patch_1":
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="continue",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["patched_plan_continues"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Patch batch completed successfully.",
            )

        if batch_id == "batch_2":
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="manual_review",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=True,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["manual_review_required"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Manual review required before final batch.",
            )

        raise AssertionError(f"Unexpected batch_id {batch_id}")

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

    assert result.status == "awaiting_manual_review"
    assert result.manual_review_required is True
    assert result.final_stage_closed is False
    assert result.completed_batches == ["batch_1", "batch_1_patch_1", "batch_2"]
    assert result.blocked_batches == ["batch_3"]

    assert len(result.iterations) == 1
    iteration = result.iterations[0]
    assert iteration.batch_ids_processed == ["batch_1", "batch_1_patch_1", "batch_2"]
    assert iteration.blocked_batch_ids_after_iteration == ["batch_3"]
    assert iteration.used_patched_plan is True
    assert (iteration.resolved_intent_type == "replan") is False
    assert iteration.requires_manual_review is True

    batch_trace_artifacts = (
        db_session.query(Artifact)
        .filter(
            Artifact.project_id == project.id,
            Artifact.artifact_type == "workflow_batch_trace",
        )
        .order_by(Artifact.id.asc())
        .all()
    )

    assert len(batch_trace_artifacts) == 3

    batch_trace_payloads = [json.loads(item.content) for item in batch_trace_artifacts]
    assert [payload["batch_id"] for payload in batch_trace_payloads] == [
        "batch_1",
        "batch_1_patch_1",
        "batch_2",
    ]

    assert batch_trace_payloads[0]["patched_plan_version"] == 100
    assert batch_trace_payloads[0]["resolved_intent_type"] == "assign"

    assert batch_trace_payloads[1]["patched_plan_version"] is None
    assert batch_trace_payloads[1]["resolved_intent_type"] == "continue"

    assert batch_trace_payloads[2]["patched_plan_version"] is None
    assert batch_trace_payloads[2]["resolved_intent_type"] == "manual_review"

    assert iteration.blocked_batch_ids_after_iteration == result.blocked_batches == ["batch_3"]


def test_workflow_completed_batches_never_contains_duplicates(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow completed batches uniqueness invariant",
        description="Project for validating that completed_batches never contains duplicates",
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

    plan = make_execution_plan(
        plan_version=110,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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

    generation_calls = []

    monkeypatch.setattr(
        "app.services.project_workflow_service.generate_execution_plan",
        lambda db, project_id: generation_calls.append("generate") or plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    def _fake_execute_task_sync(db, task_id):
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

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
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="resequence",
                resolved_mutation_scope="resequence",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["remaining_plan_still_valid"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Deferred resequencing after batch 1.",
            )

        if batch_id == "batch_2":
            return _post_batch_result(
                status="project_stage_closed",
                resolved_intent_type="close",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=True,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["stage_goals_satisfied"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Stage closed.",
            )

        raise AssertionError(f"Unexpected batch_id {batch_id}")

    monkeypatch.setattr(
        "app.services.project_workflow_service._process_batch_after_terminal_tasks",
        _fake_post_batch,
    )

    result = run_project_workflow(
        db=db_session,
        project_id=project.id,
        max_workflow_iterations=3,
        max_finalization_iterations=2,
    )

    assert result.status == "stage_closed"
    assert result.completed_batches == ["batch_1", "batch_2"]
    assert len(result.completed_batches) == len(set(result.completed_batches))


def test_workflow_invalidates_active_plan_and_regenerates_when_iteration_requires_replan(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow replan invalidates active plan invariant",
        description="Project for validating that structural replan invalidates active_plan",
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
    replanned_task = make_task(
        project_id=project.id,
        title="Replanned atomic task",
        status=TASK_STATUS_PENDING,
    )

    initial_plan = make_execution_plan(
        plan_version=111,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            }
        ],
    )

    replanned_plan = make_execution_plan(
        plan_version=112,
        batches=[
            {
                "batch_id": "batch_replanned",
                "task_ids": [replanned_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
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

    generation_calls = []

    def _fake_generate_execution_plan(db, project_id):
        generation_calls.append("generate")
        if len(generation_calls) == 1:
            return initial_plan
        return replanned_plan

    monkeypatch.setattr(
        "app.services.project_workflow_service.generate_execution_plan",
        _fake_generate_execution_plan,
    )
    monkeypatch.setattr(
        "app.services.project_workflow_service.persist_execution_plan",
        lambda **kwargs: None,
    )

    executed_task_ids = []

    def _fake_execute_task_sync(db, task_id):
        executed_task_ids.append(task_id)
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

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
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="replan",
                resolved_mutation_scope="replan",
                remaining_plan_still_valid=False,
                has_new_recovery_tasks=False,
                requires_plan_mutation=True,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=True,
                patched_execution_plan=None,
                decision_signals=["remaining_plan_invalid"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Structural replanning required.",
            )

        if batch_id == "batch_replanned":
            return _post_batch_result(
                status="project_stage_closed",
                resolved_intent_type="close",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=True,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["stage_goals_satisfied"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Replanned batch closed the stage.",
            )

        raise AssertionError(f"Unexpected batch_id {batch_id}")

    monkeypatch.setattr(
        "app.services.project_workflow_service._process_batch_after_terminal_tasks",
        _fake_post_batch,
    )

    result = run_project_workflow(
        db=db_session,
        project_id=project.id,
        max_workflow_iterations=3,
        max_finalization_iterations=2,
    )

    assert generation_calls == ["generate", "generate"]
    assert executed_task_ids == [task_1.id, replanned_task.id]
    assert result.status == "stage_closed"
    assert result.completed_batches == ["batch_1", "batch_replanned"]
    assert result.plan_version == 112


def test_workflow_blocked_batches_never_includes_completed_batches(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project(
        name="Workflow blocked batches exclusion invariant",
        description="Project for validating that blocked_batches excludes completed batches",
    )
    project.enable_technical_refinement = False
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task_1 = make_task(project_id=project.id, title="Atomic task 1", status=TASK_STATUS_PENDING)
    task_2 = make_task(project_id=project.id, title="Atomic task 2", status=TASK_STATUS_PENDING)
    task_3 = make_task(project_id=project.id, title="Atomic task 3", status=TASK_STATUS_PENDING)

    plan = make_execution_plan(
        plan_version=113,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [task_1.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [task_2.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_3",
                "task_ids": [task_3.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
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
        return _successful_execution_result(executor_type=EXECUTION_ENGINE)

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
            return _post_batch_result(
                status="completed_with_evaluation",
                resolved_intent_type="continue",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=True,
                should_close_stage=False,
                requires_manual_review=False,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["remaining_plan_still_valid"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Continue.",
            )

        if batch_id == "batch_2":
            return _post_batch_result(
                status="checkpoint_blocked",
                resolved_intent_type="manual_review",
                resolved_mutation_scope="none",
                remaining_plan_still_valid=True,
                has_new_recovery_tasks=False,
                requires_plan_mutation=False,
                requires_all_new_tasks_assigned=False,
                can_continue_after_application=False,
                should_close_stage=False,
                requires_manual_review=True,
                reopened_finalization=False,
                patched_execution_plan=None,
                decision_signals=["manual_review_required"],
                finalization_iteration_count=current_finalization_iteration_count,
                finalization_guard_triggered=False,
                notes="Stop before final batch.",
            )

        raise AssertionError(f"Unexpected batch_id {batch_id}")

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

    assert result.completed_batches == ["batch_1", "batch_2"]
    assert result.blocked_batches == ["batch_3"]
    assert set(result.completed_batches).isdisjoint(set(result.blocked_batches))
