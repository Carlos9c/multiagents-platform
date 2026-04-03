import json
import types

from app.execution_engine.contracts import (
    CHANGE_TYPE_MODIFIED,
    EXECUTION_DECISION_COMPLETED,
    EXECUTION_DECISION_PARTIAL,
    ChangedFile,
    CommandExecution,
    ExecutionEvidence,
    ExecutionRequest,
    ExecutionResult,
    ProjectExecutionContext,
)
from app.models.execution_run import ExecutionRun
from app.models.task import (
    EXECUTION_ENGINE,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PARTIAL,
)
from app.services.task_execution_service import execute_task_sync
from app.services.validation.contracts import (
    ResolvedValidationIntent,
    ValidationResult,
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
                promoted_state.__setitem__("called", True) if promoted_state is not None else None
            ),
        )

    monkeypatch.setattr(
        "app.services.task_execution_service.LocalWorkspaceRuntime",
        _factory,
    )


def _patch_execution_request(
    monkeypatch,
    *,
    workspace_path: str,
    source_path: str,
    allowed_paths: list[str],
    relevant_files: list[str],
):
    def _fake_build_execution_request(db, task, execution_run_id, resolved_executor_type):
        return ExecutionRequest(
            task_id=task.id,
            project_id=task.project_id,
            execution_run_id=execution_run_id,
            executor_type=resolved_executor_type,
            task_title=task.title,
            task_description=task.description,
            task_summary=task.summary,
            objective=task.objective,
            acceptance_criteria=task.acceptance_criteria,
            technical_constraints=task.technical_constraints,
            out_of_scope=task.out_of_scope,
            allowed_paths=allowed_paths,
            context=ProjectExecutionContext(
                project_id=task.project_id,
                workspace_path=workspace_path,
                source_path=source_path,
                relevant_files=relevant_files,
                key_decisions=["Preserve current public interface."],
                related_tasks=[],
            ),
        )

    monkeypatch.setattr(
        "app.services.task_execution_service.build_placeholder_execution_request",
        _fake_build_execution_request,
    )


def _patch_engine(monkeypatch, engine_result: ExecutionResult):
    def _fake_execute(db, request):
        return engine_result

    monkeypatch.setattr(
        "app.services.task_execution_service.get_execution_engine",
        lambda db: types.SimpleNamespace(
            backend_name="test_engine",
            execute=_fake_execute,
        ),
    )


def _patch_validation_service_flow(
    monkeypatch,
    *,
    validation_decision: str,
    final_task_status: str,
    followup_validation_required: bool = False,
    capture: dict | None = None,
):
    def _fake_resolve_validation_route(*, routing_input):
        if capture is not None:
            capture["routing_input"] = routing_input
        return ResolvedValidationIntent(
            validator_key="code_task_validator",
            discipline="code",
            validation_mode="post_execution",
            requires_workspace=True,
            requires_artifacts=True,
            requires_changed_files=True,
            requires_commands=True,
            requires_execution_context=True,
            requires_output_snapshot=True,
            requires_agent_sequence=True,
            requires_file_reading=True,
            notes=[],
        )

    def _fake_dispatch_validation(*, intent, validation_input):
        if capture is not None:
            capture["intent"] = intent
            capture["validation_input"] = validation_input
        return ValidationResult(
            validator_key="code_task_validator",
            discipline="code",
            decision=validation_decision,
            summary="Validation summary.",
            findings=[],
            validated_scope="Validated scope." if validation_decision == "completed" else None,
            missing_scope="Missing scope." if validation_decision != "completed" else None,
            blockers=[],
            manual_review_required=(validation_decision == "manual_review"),
            final_task_status=final_task_status,
            artifacts_created=[],
            validated_evidence_ids=["produced_file:app_service.py", "command:0"],
            unconsumed_evidence_ids=[],
            followup_validation_required=followup_validation_required,
            recommended_next_validator_keys=[],
            partial_validation_summary=(
                "Additional validation follow-up required."
                if followup_validation_required
                else None
            ),
            metadata={"confidence": "high"},
        )

    monkeypatch.setattr(
        "app.services.validation.service.resolve_validation_route",
        _fake_resolve_validation_route,
    )
    monkeypatch.setattr(
        "app.services.validation.service.dispatch_validation",
        _fake_dispatch_validation,
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


def test_execute_task_sync_vertical_flow_completed_validation_completes_task(
    tmp_path,
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement service behavior",
        description="Update service implementation and tests.",
        objective="Deliver the requested service behavior.",
        acceptance_criteria="Implementation and tests updated.",
        technical_constraints="Keep the current module structure.",
        out_of_scope="No deployment changes.",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()

    (workspace_dir / "app_service.py").write_text(
        "def run():\n    return 'ok'\n",
        encoding="utf-8",
    )

    promoted = {"called": False}
    expected_sequence = [
        "context_selection_agent",
        "code_change_agent",
        "command_runner_agent",
    ]
    captured = {}

    _patch_workspace_runtime(monkeypatch, promoted_state=promoted)

    _patch_execution_request(
        monkeypatch,
        workspace_path=str(workspace_dir),
        source_path=str(source_dir),
        allowed_paths=["app_service.py"],
        relevant_files=["app_service.py"],
    )

    engine_result = ExecutionResult(
        task_id=task.id,
        decision=EXECUTION_DECISION_COMPLETED,
        summary="Execution completed successfully.",
        details="Updated service implementation.",
        completed_scope="Updated service behavior and related implementation.",
        remaining_scope=None,
        blockers_found=[],
        validation_notes=["Execution completed without blockers."],
        output_snapshot="done",
        execution_agent_sequence=expected_sequence,
        evidence=ExecutionEvidence(
            changed_files=[
                ChangedFile(
                    path="app_service.py",
                    change_type=CHANGE_TYPE_MODIFIED,
                )
            ],
            commands=[
                CommandExecution(
                    command="pytest -q",
                    exit_code=0,
                    stdout="1 passed",
                    stderr="",
                )
            ],
            notes=["Observed repository changes."],
            artifacts_created=[],
        ),
    )

    _patch_engine(monkeypatch, engine_result)
    _patch_validation_service_flow(
        monkeypatch,
        validation_decision="completed",
        final_task_status=TASK_STATUS_COMPLETED,
        capture=captured,
    )

    result = execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    latest_run = _get_latest_run_for_task(db_session, task.id)

    assert result.executor_type == EXECUTION_ENGINE
    assert result.final_task_status == TASK_STATUS_COMPLETED
    assert result.validation_decision == "completed"
    assert task.status == TASK_STATUS_COMPLETED
    assert promoted["called"] is True
    assert json.loads(task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(latest_run.execution_agent_sequence) == expected_sequence

    assert captured["routing_input"].task.task_id == task.id
    assert captured["validation_input"].task.task_id == task.id
    assert captured["validation_input"].evidence_package.evidence_items
    assert captured["validation_input"].request_context.allowed_paths == ["app_service.py"]


def test_execute_task_sync_vertical_flow_partial_validation_marks_task_partial(
    tmp_path,
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement service behavior partially",
        description="Update service implementation but leave some work pending.",
        objective="Advance the requested service behavior.",
        acceptance_criteria="Implementation is advanced and remaining scope is identified.",
        technical_constraints="Keep the current module structure.",
        out_of_scope="No deployment changes.",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()

    (workspace_dir / "app_service.py").write_text(
        "def run():\n    return 'partial'\n",
        encoding="utf-8",
    )

    promoted = {"called": False}
    expected_sequence = ["context_selection_agent", "code_change_agent"]
    captured = {}

    _patch_workspace_runtime(monkeypatch, promoted_state=promoted)

    _patch_execution_request(
        monkeypatch,
        workspace_path=str(workspace_dir),
        source_path=str(source_dir),
        allowed_paths=["app_service.py"],
        relevant_files=["app_service.py"],
    )

    engine_result = ExecutionResult(
        task_id=task.id,
        decision=EXECUTION_DECISION_PARTIAL,
        summary="Execution completed partially.",
        details="Implemented only part of the requested behavior.",
        completed_scope="Implemented part of the requested service behavior.",
        remaining_scope="Finish the remaining branch and supporting checks.",
        blockers_found=[],
        validation_notes=["Execution completed with remaining scope."],
        output_snapshot="partial",
        execution_agent_sequence=expected_sequence,
        evidence=ExecutionEvidence(
            changed_files=[
                ChangedFile(
                    path="app_service.py",
                    change_type=CHANGE_TYPE_MODIFIED,
                )
            ],
            commands=[
                CommandExecution(
                    command="pytest -q",
                    exit_code=0,
                    stdout="1 passed",
                    stderr="",
                )
            ],
            notes=["Observed repository changes."],
            artifacts_created=[],
        ),
    )

    _patch_engine(monkeypatch, engine_result)
    _patch_validation_service_flow(
        monkeypatch,
        validation_decision="partial",
        final_task_status=TASK_STATUS_PARTIAL,
        capture=captured,
    )

    result = execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    latest_run = _get_latest_run_for_task(db_session, task.id)

    assert result.executor_type == EXECUTION_ENGINE
    assert result.final_task_status == TASK_STATUS_PARTIAL
    assert result.validation_decision == "partial"
    assert task.status == TASK_STATUS_PARTIAL
    assert promoted["called"] is False
    assert json.loads(task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(latest_run.execution_agent_sequence) == expected_sequence

    assert captured["routing_input"].task.task_id == task.id
    assert captured["validation_input"].task.task_id == task.id
    assert captured["validation_input"].evidence_package.evidence_items
    assert captured["validation_input"].request_context.allowed_paths == ["app_service.py"]
