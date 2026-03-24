import types

import pytest

from app.models.task import (
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    Task,
)
from app.schemas.code_execution import (
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_EXECUTION_STATUS_FAILED,
    CODE_EXECUTION_STATUS_REJECTED,
    CodeExecutorInput,
    CodeExecutorResult,
    CodeFileEditPlan,
    CodeWorkingSet,
    ExecutionJournal,
    WorkspaceChangeSet,
)
from app.services.task_execution_service import (
    TaskExecutionServiceError,
    execute_task_sync,
)


def _build_executor_result(*, task_id: int, execution_status: str, summary: str = "Execution summary.") -> CodeExecutorResult:
    return CodeExecutorResult(
        task_id=task_id,
        execution_status=execution_status,
        input=CodeExecutorInput(
            task_id=task_id,
            project_id=1,
            title="Atomic task",
            description="Atomic task description.",
            objective="Atomic task objective.",
            acceptance_criteria="Must satisfy expected behavior.",
            technical_constraints=None,
            out_of_scope=None,
            execution_goal="Complete the atomic task.",
            repo_root=".",
            relevant_decisions=[],
            candidate_modules=[],
            candidate_files=[],
            primary_targets=[],
            related_files=[],
            reference_files=[],
            related_test_files=[],
            relevant_symbols=[],
            unresolved_questions=[],
            selection_rationale="Test rationale.",
            selection_confidence=1.0,
        ),
        working_set=CodeWorkingSet(
            repo_root=".",
            target_files=[],
            related_files=[],
            reference_files=[],
            test_files=[],
            files=[],
            repo_guidance=[],
        ),
        edit_plan=CodeFileEditPlan(
            task_id=task_id,
            summary="Planned changes summary.",
            planned_changes=[],
            assumptions=[],
            local_risks=[],
            notes=[],
        ),
        workspace_changes=WorkspaceChangeSet(
            created_files=[],
            modified_files=[],
            deleted_files=[],
            renamed_files=[],
            diff_summary=None,
            impacted_areas=[],
        ),
        journal=ExecutionJournal(
            task_id=task_id,
            summary=summary,
            local_decisions=[],
            claimed_completed_scope="Completed scope." if execution_status == CODE_EXECUTION_STATUS_AWAITING_VALIDATION else None,
            claimed_remaining_scope="Remaining scope." if execution_status != CODE_EXECUTION_STATUS_AWAITING_VALIDATION else None,
            encountered_uncertainties=[],
            notes_for_validator=[],
        ),
        output_snapshot="executor output",
    )


def _set_task_status(db, task_id: int, status: str) -> str:
    task = db.get(Task, task_id)
    task.status = status
    db.add(task)
    db.commit()
    db.refresh(task)
    return task.status


def test_execute_task_sync_rejects_non_atomic_task(
    db_session,
    make_project,
    make_task,
):
    project = make_project()
    non_atomic_task = make_task(
        project_id=project.id,
        title="High-level task",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        status=TASK_STATUS_PENDING,
        executor_type="pending_atomic_assignment",
    )

    with pytest.raises(TaskExecutionServiceError, match="Only atomic tasks can be executed"):
        execute_task_sync(db_session, non_atomic_task.id)


def test_execute_task_sync_completed_reconciles_parent_and_promotes_workspace(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()

    parent = make_task(
        project_id=project.id,
        title="Parent task",
        planning_level="high_level",
        status=TASK_STATUS_PENDING,
        executor_type="pending_atomic_assignment",
    )
    atomic_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Atomic implementation task",
        status=TASK_STATUS_PENDING,
    )

    promoted = {"called": False}

    executor_result = _build_executor_result(
        task_id=atomic_task.id,
        execution_status=CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
        summary="Execution finished successfully.",
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.LocalCodeExecutor",
        lambda db: types.SimpleNamespace(
            execute=lambda task, execution_run_id: executor_result
        ),
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.validate_code_task",
        lambda **kwargs: types.SimpleNamespace(
            validation_result=types.SimpleNamespace(
                decision="completed",
            )
        ),
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.apply_validation_decision_to_task",
        lambda db, task_id, decision: _set_task_status(db, task_id, TASK_STATUS_COMPLETED),
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.LocalWorkspaceRuntime",
        lambda storage_service: types.SimpleNamespace(
            promote_workspace_to_source=lambda **kwargs: promoted.__setitem__("called", True)
        ),
    )

    result = execute_task_sync(db_session, atomic_task.id)

    db_session.refresh(atomic_task)
    db_session.refresh(parent)

    assert result.final_task_status == TASK_STATUS_COMPLETED
    assert result.validation_decision == "completed"
    assert atomic_task.status == TASK_STATUS_COMPLETED
    assert parent.status == TASK_STATUS_COMPLETED
    assert promoted["called"] is True


def test_execute_task_sync_partial_reconciles_parent_to_partial(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()

    parent = make_task(
        project_id=project.id,
        title="Parent task",
        planning_level="high_level",
        status=TASK_STATUS_PENDING,
        executor_type="pending_atomic_assignment",
    )
    atomic_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Atomic implementation task",
        status=TASK_STATUS_PENDING,
    )

    executor_result = _build_executor_result(
        task_id=atomic_task.id,
        execution_status=CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
        summary="Execution finished but validation will be partial.",
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.LocalCodeExecutor",
        lambda db: types.SimpleNamespace(
            execute=lambda task, execution_run_id: executor_result
        ),
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.validate_code_task",
        lambda **kwargs: types.SimpleNamespace(
            validation_result=types.SimpleNamespace(
                decision="partial",
            )
        ),
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.apply_validation_decision_to_task",
        lambda db, task_id, decision: _set_task_status(db, task_id, TASK_STATUS_PARTIAL),
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.LocalWorkspaceRuntime",
        lambda storage_service: types.SimpleNamespace(
            promote_workspace_to_source=lambda **kwargs: None
        ),
    )

    result = execute_task_sync(db_session, atomic_task.id)

    db_session.refresh(atomic_task)
    db_session.refresh(parent)

    assert result.final_task_status == TASK_STATUS_PARTIAL
    assert result.validation_decision == "partial"
    assert atomic_task.status == TASK_STATUS_PARTIAL
    assert parent.status == TASK_STATUS_PARTIAL


def test_execute_task_sync_failed_terminal_path_reconciles_parent_to_failed(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()

    parent = make_task(
        project_id=project.id,
        title="Parent task",
        planning_level="high_level",
        status=TASK_STATUS_PENDING,
        executor_type="pending_atomic_assignment",
    )
    atomic_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Atomic task that fails",
        status=TASK_STATUS_PENDING,
    )

    executor_result = _build_executor_result(
        task_id=atomic_task.id,
        execution_status=CODE_EXECUTION_STATUS_FAILED,
        summary="Executor reported a failed execution.",
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.LocalCodeExecutor",
        lambda db: types.SimpleNamespace(
            execute=lambda task, execution_run_id: executor_result
        ),
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.validate_terminal_code_task",
        lambda **kwargs: types.SimpleNamespace(
            final_task_status=TASK_STATUS_FAILED,
            validation_result=types.SimpleNamespace(
                decision="failed",
            ),
        ),
    )

    result = execute_task_sync(db_session, atomic_task.id)

    db_session.refresh(atomic_task)
    db_session.refresh(parent)

    assert result.final_task_status == TASK_STATUS_FAILED
    assert result.validation_decision == "failed"
    assert atomic_task.status == TASK_STATUS_FAILED
    assert parent.status == TASK_STATUS_FAILED


def test_execute_task_sync_rejected_terminal_path_keeps_original_terminal(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()

    parent = make_task(
        project_id=project.id,
        title="Parent task",
        planning_level="high_level",
        status=TASK_STATUS_PENDING,
        executor_type="pending_atomic_assignment",
    )
    atomic_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Atomic task rejected by executor",
        status=TASK_STATUS_PENDING,
    )

    executor_result = _build_executor_result(
        task_id=atomic_task.id,
        execution_status=CODE_EXECUTION_STATUS_REJECTED,
        summary="Executor rejected the task.",
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.LocalCodeExecutor",
        lambda db: types.SimpleNamespace(
            execute=lambda task, execution_run_id: executor_result
        ),
    )

    monkeypatch.setattr(
        "app.services.task_execution_service.validate_terminal_code_task",
        lambda **kwargs: types.SimpleNamespace(
            final_task_status=TASK_STATUS_FAILED,
            validation_result=types.SimpleNamespace(
                decision="failed",
            ),
        ),
    )

    result = execute_task_sync(db_session, atomic_task.id)

    db_session.refresh(atomic_task)
    db_session.refresh(parent)

    assert result.run_status == CODE_EXECUTION_STATUS_REJECTED
    assert result.final_task_status == TASK_STATUS_FAILED
    assert atomic_task.status == TASK_STATUS_FAILED
    assert parent.status == TASK_STATUS_FAILED