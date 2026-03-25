import json
import types

import pytest

from app.execution_engine.contracts import (
    EXECUTION_DECISION_FAILED,
    EXECUTION_DECISION_PARTIAL,
    EXECUTION_DECISION_REJECTED,
    ExecutionEvidence,
    ExecutionResult,
)
from app.models.execution_run import ExecutionRun
from app.models.task import (
    CODE_EXECUTOR,
    PENDING_ENGINE_ROUTING_EXECUTOR,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    Task,
)
from app.schemas.code_execution import (
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_EXECUTION_STATUS_REJECTED,
)
from app.services.task_execution_service import (
    TaskExecutionServiceError,
    execute_task_sync,
)


def _build_engine_result(
    *,
    task_id: int,
    decision: str,
    summary: str = "Execution summary.",
    execution_agent_sequence: list[str] | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        task_id=task_id,
        decision=decision,
        summary=summary,
        details="Execution details.",
        completed_scope="Completed scope." if decision == EXECUTION_DECISION_PARTIAL else None,
        remaining_scope="Remaining scope." if decision != EXECUTION_DECISION_PARTIAL else None,
        blockers_found=[],
        validation_notes=[],
        output_snapshot="executor output",
        execution_agent_sequence=execution_agent_sequence or [],
        evidence=ExecutionEvidence(
            changed_files=[],
            commands=[],
            notes=[],
            artifacts_created=[],
        ),
    )


def _set_task_status(db, task_id: int, status: str) -> str:
    task = db.get(Task, task_id)
    task.status = status
    db.add(task)
    db.commit()
    db.refresh(task)
    return task.status


def _patch_engine(monkeypatch, engine_result: ExecutionResult, *, captured_request: dict | None = None):
    def _fake_build_execution_request(db, task, execution_run_id, resolved_executor_type):
        request = types.SimpleNamespace(
            task_id=task.id,
            project_id=task.project_id,
            execution_run_id=execution_run_id,
            task_title=task.title,
            task_description=task.description,
            task_summary=task.summary,
            objective=task.objective,
            acceptance_criteria=task.acceptance_criteria,
            technical_constraints=task.technical_constraints,
            out_of_scope=task.out_of_scope,
            executor_type=resolved_executor_type,
            context=types.SimpleNamespace(
                workspace_path=".",
                source_path=".",
                relevant_files=[],
                key_decisions=[],
                related_tasks=[],
            ),
            allowed_paths=[],
            blocked_paths=[],
        )
        if captured_request is not None:
            captured_request["request"] = request
        return request

    monkeypatch.setattr(
        "app.services.task_execution_service.build_execution_request",
        _fake_build_execution_request,
    )
    monkeypatch.setattr(
        "app.services.task_execution_service.get_execution_engine",
        lambda db: types.SimpleNamespace(
            backend_name="test_engine",
            execute=lambda request: engine_result,
        ),
    )


def _patch_workspace_runtime(monkeypatch, *, promoted_state: dict | None = None):
    def _factory(storage_service):
        return types.SimpleNamespace(
            prepare_workspace=lambda **kwargs: types.SimpleNamespace(
                workspace_dir=".",
                source_dir=".",
                logs_dir=".",
                outputs_dir=".",
            ),
            promote_workspace_to_source=lambda **kwargs: (
                promoted_state.__setitem__("called", True)
                if promoted_state is not None
                else None
            ),
        )

    monkeypatch.setattr(
        "app.services.task_execution_service.LocalWorkspaceRuntime",
        _factory,
    )


def _get_latest_run_for_task(db, task_id: int) -> ExecutionRun:
    run = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )
    assert run is not None
    return run


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
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )

    with pytest.raises(TaskExecutionServiceError, match="Only atomic tasks can be executed"):
        execute_task_sync(db_session, non_atomic_task.id)


def test_execute_task_sync_resolves_pending_executor_at_runtime(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    atomic_task = make_task(
        project_id=project.id,
        title="Atomic task with unresolved executor",
        status=TASK_STATUS_PENDING,
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )

    captured = {}
    expected_sequence = ["context_selection_agent", "code_change_agent"]

    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=atomic_task.id,
            decision=EXECUTION_DECISION_PARTIAL,
            summary="Execution finished successfully.",
            execution_agent_sequence=expected_sequence,
        ),
        captured_request=captured,
    )
    _patch_workspace_runtime(monkeypatch)

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

    result = execute_task_sync(db_session, atomic_task.id)

    assert captured["request"].executor_type == CODE_EXECUTOR
    assert result.executor_type == CODE_EXECUTOR

    db_session.refresh(atomic_task)
    latest_run = _get_latest_run_for_task(db_session, atomic_task.id)

    assert atomic_task.executor_type == PENDING_ENGINE_ROUTING_EXECUTOR
    assert json.loads(atomic_task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(latest_run.execution_agent_sequence) == expected_sequence


def test_execute_task_sync_accepts_legacy_resolved_executor_for_compatibility(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    atomic_task = make_task(
        project_id=project.id,
        title="Atomic task with explicit executor",
        status=TASK_STATUS_PENDING,
        executor_type=CODE_EXECUTOR,
    )

    captured = {}
    expected_sequence = ["code_change_agent"]

    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=atomic_task.id,
            decision=EXECUTION_DECISION_PARTIAL,
            summary="Execution finished successfully.",
            execution_agent_sequence=expected_sequence,
        ),
        captured_request=captured,
    )
    _patch_workspace_runtime(monkeypatch)

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

    result = execute_task_sync(db_session, atomic_task.id)

    assert captured["request"].executor_type == CODE_EXECUTOR
    assert result.executor_type == CODE_EXECUTOR

    db_session.refresh(atomic_task)
    latest_run = _get_latest_run_for_task(db_session, atomic_task.id)

    assert json.loads(atomic_task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(latest_run.execution_agent_sequence) == expected_sequence


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
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )
    atomic_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Atomic implementation task",
        status=TASK_STATUS_PENDING,
        executor_type=CODE_EXECUTOR,
    )

    promoted = {"called": False}
    expected_sequence = ["context_selection_agent", "placement_resolver_agent", "code_change_agent"]

    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=atomic_task.id,
            decision=EXECUTION_DECISION_PARTIAL,
            summary="Execution finished successfully.",
            execution_agent_sequence=expected_sequence,
        ),
    )
    _patch_workspace_runtime(monkeypatch, promoted_state=promoted)

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

    result = execute_task_sync(db_session, atomic_task.id)

    db_session.refresh(atomic_task)
    db_session.refresh(parent)
    latest_run = _get_latest_run_for_task(db_session, atomic_task.id)

    assert result.run_status == CODE_EXECUTION_STATUS_AWAITING_VALIDATION
    assert result.final_task_status == TASK_STATUS_COMPLETED
    assert result.validation_decision == "completed"
    assert result.executor_type == CODE_EXECUTOR
    assert atomic_task.status == TASK_STATUS_COMPLETED
    assert parent.status == TASK_STATUS_COMPLETED
    assert promoted["called"] is True
    assert json.loads(atomic_task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(latest_run.execution_agent_sequence) == expected_sequence


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
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )
    atomic_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Atomic implementation task",
        status=TASK_STATUS_PENDING,
        executor_type=CODE_EXECUTOR,
    )

    expected_sequence = ["context_selection_agent", "command_runner_agent"]

    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=atomic_task.id,
            decision=EXECUTION_DECISION_PARTIAL,
            summary="Execution finished but validation will be partial.",
            execution_agent_sequence=expected_sequence,
        ),
    )
    _patch_workspace_runtime(monkeypatch)

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

    result = execute_task_sync(db_session, atomic_task.id)

    db_session.refresh(atomic_task)
    db_session.refresh(parent)
    latest_run = _get_latest_run_for_task(db_session, atomic_task.id)

    assert result.run_status == CODE_EXECUTION_STATUS_AWAITING_VALIDATION
    assert result.final_task_status == TASK_STATUS_PARTIAL
    assert result.validation_decision == "partial"
    assert result.executor_type == CODE_EXECUTOR
    assert atomic_task.status == TASK_STATUS_PARTIAL
    assert parent.status == TASK_STATUS_PARTIAL
    assert json.loads(atomic_task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(latest_run.execution_agent_sequence) == expected_sequence


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
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )
    atomic_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Atomic task that fails",
        status=TASK_STATUS_PENDING,
        executor_type=CODE_EXECUTOR,
    )

    expected_sequence = ["context_selection_agent", "code_change_agent"]

    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=atomic_task.id,
            decision=EXECUTION_DECISION_FAILED,
            summary="Execution engine reported a failed execution.",
            execution_agent_sequence=expected_sequence,
        ),
    )
    _patch_workspace_runtime(monkeypatch)

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
    latest_run = _get_latest_run_for_task(db_session, atomic_task.id)

    assert result.final_task_status == TASK_STATUS_FAILED
    assert result.validation_decision == "failed"
    assert result.executor_type == CODE_EXECUTOR
    assert atomic_task.status == TASK_STATUS_FAILED
    assert parent.status == TASK_STATUS_FAILED
    assert json.loads(atomic_task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(latest_run.execution_agent_sequence) == expected_sequence


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
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
    )
    atomic_task = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Atomic task rejected by executor",
        status=TASK_STATUS_PENDING,
        executor_type=CODE_EXECUTOR,
    )

    expected_sequence = ["context_selection_agent", "command_runner_agent"]

    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=atomic_task.id,
            decision=EXECUTION_DECISION_REJECTED,
            summary="Execution engine rejected the task.",
            execution_agent_sequence=expected_sequence,
        ),
    )
    _patch_workspace_runtime(monkeypatch)

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
    latest_run = _get_latest_run_for_task(db_session, atomic_task.id)

    assert result.run_status == CODE_EXECUTION_STATUS_REJECTED
    assert result.final_task_status == TASK_STATUS_FAILED
    assert result.executor_type == CODE_EXECUTOR
    assert atomic_task.status == TASK_STATUS_FAILED
    assert parent.status == TASK_STATUS_FAILED
    assert json.loads(atomic_task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(latest_run.execution_agent_sequence) == expected_sequence