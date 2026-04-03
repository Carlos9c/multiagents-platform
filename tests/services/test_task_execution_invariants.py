import json
import types

import pytest

from app.execution_engine.contracts import (
    CHANGE_TYPE_MODIFIED,
    EXECUTION_DECISION_COMPLETED,
    EXECUTION_DECISION_FAILED,
    EXECUTION_DECISION_PARTIAL,
    EXECUTION_DECISION_REJECTED,
    ChangedFile,
    CommandExecution,
    ExecutionEvidence,
    ExecutionRequest,
    ExecutionResult,
    ProjectExecutionContext,
)
from app.models.artifact import Artifact
from app.models.execution_run import (
    EXECUTION_RUN_STATUS_FAILED,
    EXECUTION_RUN_STATUS_REJECTED,
    EXECUTION_RUN_STATUS_SUCCEEDED,
    ExecutionRun,
)
from app.models.task import (
    EXECUTION_ENGINE,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
)
from app.services.task_execution_service import (
    VALIDATION_RESULT_ARTIFACT_TYPE,
    TaskExecutionServiceError,
    execute_task_sync,
)
from app.services.validation.contracts import (
    ResolvedValidationIntent,
    ValidationResult,
)
from app.services.validation.service import ValidationServiceError


def _patch_workspace_runtime(
    monkeypatch,
    *,
    promoted_state: dict | None = None,
    fail_on_promote: bool = False,
):
    def _promote_workspace_to_source(**kwargs):
        if fail_on_promote:
            raise RuntimeError("promotion failed")
        if promoted_state is not None:
            promoted_state["called"] = True

    def _factory(storage_service):
        return types.SimpleNamespace(
            prepare_workspace=lambda **kwargs: types.SimpleNamespace(
                workspace_dir=".",
                source_dir=".",
                logs_dir=".",
                outputs_dir=".",
            ),
            promote_workspace_to_source=_promote_workspace_to_source,
        )

    monkeypatch.setattr(
        "app.services.task_execution_service.LocalWorkspaceRuntime",
        _factory,
    )


def _patch_execution_request(
    monkeypatch,
    *,
    workspace_path: str = ".",
    source_path: str = ".",
    allowed_paths: list[str] | None = None,
    relevant_files: list[str] | None = None,
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
            allowed_paths=list(allowed_paths or []),
            context=ProjectExecutionContext(
                project_id=task.project_id,
                workspace_path=workspace_path,
                source_path=source_path,
                relevant_files=list(relevant_files or []),
                key_decisions=["Keep the public interface stable."],
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


def _patch_validation_service(
    monkeypatch,
    *,
    validation_decision: str,
    final_task_status: str,
    followup_validation_required: bool = False,
):
    def _fake_resolve_validation_route(*, routing_input):
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


def _patch_validation_service_to_raise(monkeypatch):
    def _fake_validate_execution_result(**kwargs):
        raise ValidationServiceError("validator crashed")

    monkeypatch.setattr(
        "app.services.task_execution_service.validate_execution_result",
        _fake_validate_execution_result,
    )


def _build_engine_result(
    *,
    task_id: int,
    decision: str,
    execution_agent_sequence: list[str] | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        task_id=task_id,
        decision=decision,
        summary="Execution summary.",
        details="Execution details.",
        completed_scope=(
            "Completed scope."
            if decision in {EXECUTION_DECISION_COMPLETED, EXECUTION_DECISION_PARTIAL}
            else None
        ),
        remaining_scope=(
            "Remaining scope."
            if decision in {EXECUTION_DECISION_PARTIAL, EXECUTION_DECISION_FAILED}
            else None
        ),
        blockers_found=[],
        validation_notes=["Execution notes."],
        output_snapshot="executor output",
        execution_agent_sequence=execution_agent_sequence or [],
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


def _get_latest_run_for_task(db, task_id: int) -> ExecutionRun:
    run = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )
    assert run is not None
    return run


def _get_validation_artifacts_for_task(db, task_id: int) -> list[Artifact]:
    return (
        db.query(Artifact)
        .filter(
            Artifact.task_id == task_id,
            Artifact.artifact_type == VALIDATION_RESULT_ARTIFACT_TYPE,
        )
        .order_by(Artifact.id.asc())
        .all()
    )


def test_invariant_completed_execution_creates_single_validation_artifact_and_promotes_workspace(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement service behavior",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    promoted = {"called": False}
    expected_sequence = [
        "context_selection_agent",
        "code_change_agent",
        "command_runner_agent",
    ]

    _patch_workspace_runtime(monkeypatch, promoted_state=promoted)
    _patch_execution_request(
        monkeypatch,
        workspace_path="workspace",
        source_path="source",
        allowed_paths=["app_service.py"],
        relevant_files=["app_service.py"],
    )
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=expected_sequence,
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="completed",
        final_task_status=TASK_STATUS_COMPLETED,
    )

    result = execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert result.run_status == EXECUTION_RUN_STATUS_SUCCEEDED
    assert result.final_task_status == TASK_STATUS_COMPLETED
    assert result.validation_decision == "completed"

    assert task.status == TASK_STATUS_COMPLETED
    assert run.status == EXECUTION_RUN_STATUS_SUCCEEDED
    assert promoted["called"] is True

    assert json.loads(task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(run.execution_agent_sequence) == expected_sequence

    assert len(validation_artifacts) == 1

    payload = json.loads(validation_artifacts[0].content)
    assert payload["decision"] == "completed"
    assert payload["final_task_status"] == TASK_STATUS_COMPLETED
    assert payload["workspace_promoted_to_source"] is True
    assert payload["execution_run_id"] == run.id
    assert payload["task_id"] == task.id


def test_invariant_partial_execution_persists_validation_artifact_without_workspace_promotion(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement partially",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    promoted = {"called": False}
    expected_sequence = ["context_selection_agent", "code_change_agent"]

    _patch_workspace_runtime(monkeypatch, promoted_state=promoted)
    _patch_execution_request(
        monkeypatch,
        allowed_paths=["app_service.py"],
        relevant_files=["app_service.py"],
    )
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_PARTIAL,
            execution_agent_sequence=expected_sequence,
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="partial",
        final_task_status=TASK_STATUS_PARTIAL,
        followup_validation_required=True,
    )

    result = execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert result.run_status == EXECUTION_RUN_STATUS_SUCCEEDED
    assert result.final_task_status == TASK_STATUS_PARTIAL
    assert result.validation_decision == "partial"

    assert task.status == TASK_STATUS_PARTIAL
    assert run.status == EXECUTION_RUN_STATUS_SUCCEEDED
    assert promoted["called"] is False

    assert json.loads(task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(run.execution_agent_sequence) == expected_sequence

    assert len(validation_artifacts) == 1
    payload = json.loads(validation_artifacts[0].content)
    assert payload["decision"] == "partial"
    assert payload["final_task_status"] == TASK_STATUS_PARTIAL
    assert payload["workspace_promoted_to_source"] is False
    assert payload["followup_validation_required"] is True


@pytest.mark.parametrize(
    ("engine_decision", "expected_run_status"),
    [
        (EXECUTION_DECISION_FAILED, EXECUTION_RUN_STATUS_FAILED),
        (EXECUTION_DECISION_REJECTED, EXECUTION_RUN_STATUS_REJECTED),
    ],
)
def test_invariant_non_validable_terminal_outcomes_skip_validation_artifacts_and_workspace_promotion(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    engine_decision,
    expected_run_status,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Terminal non-validable task",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    promoted = {"called": False}
    expected_sequence = ["context_selection_agent", "router_agent"]

    _patch_workspace_runtime(monkeypatch, promoted_state=promoted)
    _patch_execution_request(monkeypatch)
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=engine_decision,
            execution_agent_sequence=expected_sequence,
        ),
    )

    result = execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert result.final_task_status == TASK_STATUS_FAILED
    assert result.validation_decision is None

    assert task.status == TASK_STATUS_FAILED
    assert run.status == expected_run_status
    assert promoted["called"] is False

    assert json.loads(task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(run.execution_agent_sequence) == expected_sequence

    assert validation_artifacts == []


def test_validation_service_failure_marks_execution_run_failed(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Validation crash should degrade run state",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    _patch_workspace_runtime(monkeypatch)
    _patch_execution_request(monkeypatch)
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=["context_selection_agent", "code_change_agent"],
        ),
    )
    _patch_validation_service_to_raise(monkeypatch)

    with pytest.raises(TaskExecutionServiceError, match="validation could not be completed"):
        execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert task.status == TASK_STATUS_FAILED
    assert run.status == EXECUTION_RUN_STATUS_FAILED
    assert validation_artifacts == []


def test_workspace_promotion_failure_marks_execution_run_failed(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Promotion crash should degrade run state",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    _patch_workspace_runtime(monkeypatch, fail_on_promote=True)
    _patch_execution_request(monkeypatch)
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=["context_selection_agent", "code_change_agent"],
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="completed",
        final_task_status=TASK_STATUS_COMPLETED,
    )

    with pytest.raises(
        TaskExecutionServiceError,
        match="workspace could not be promoted to source",
    ):
        execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert task.status == TASK_STATUS_FAILED
    assert run.status == EXECUTION_RUN_STATUS_FAILED
    assert validation_artifacts == []


def test_validation_artifact_persistence_failure_marks_task_and_run_failed(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Artifact persistence failure should degrade orchestration",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    _patch_workspace_runtime(monkeypatch)
    _patch_execution_request(monkeypatch)
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=["context_selection_agent", "code_change_agent"],
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="completed",
        final_task_status=TASK_STATUS_COMPLETED,
    )

    def _boom(**kwargs):
        raise RuntimeError("artifact persistence failed")

    monkeypatch.setattr(
        "app.services.task_execution_service._persist_validation_result_artifact",
        _boom,
    )

    with pytest.raises(
        TaskExecutionServiceError,
        match="post-validation processing failed|artifact persistence failed",
    ):
        execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert task.status == TASK_STATUS_FAILED
    assert run.status == EXECUTION_RUN_STATUS_FAILED
    assert validation_artifacts == []


def test_hierarchy_reconciliation_failure_does_not_reopen_or_degrade_closed_child_task(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Hierarchy reconciliation failure happens after child closure",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    _patch_workspace_runtime(monkeypatch)
    _patch_execution_request(monkeypatch)
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=["context_selection_agent", "code_change_agent"],
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="completed",
        final_task_status=TASK_STATUS_COMPLETED,
    )

    def _boom(**kwargs):
        raise TaskExecutionServiceError("hierarchy reconciliation failed")

    monkeypatch.setattr(
        "app.services.task_execution_service._reconcile_hierarchy_or_raise",
        _boom,
    )

    with pytest.raises(TaskExecutionServiceError, match="hierarchy reconciliation failed"):
        execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert task.status == TASK_STATUS_COMPLETED
    assert run.status == EXECUTION_RUN_STATUS_SUCCEEDED
    assert len(validation_artifacts) == 1

    payload = json.loads(validation_artifacts[0].content)
    assert payload["decision"] == "completed"
    assert payload["final_task_status"] == TASK_STATUS_COMPLETED


def test_failure_path_degrades_task_and_run_even_if_reconciliation_also_fails(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Failure degradation must survive reconciliation errors",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    _patch_workspace_runtime(monkeypatch)
    _patch_execution_request(monkeypatch)
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=["context_selection_agent", "code_change_agent"],
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="completed",
        final_task_status=TASK_STATUS_COMPLETED,
    )

    monkeypatch.setattr(
        "app.services.task_execution_service._persist_validation_result_artifact",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("artifact persistence failed")),
    )

    monkeypatch.setattr(
        "app.services.task_execution_service._reconcile_hierarchy_or_raise",
        lambda **kwargs: (_ for _ in ()).throw(TaskExecutionServiceError("reconcile failed")),
    )

    with pytest.raises(
        TaskExecutionServiceError,
        match="post-validation processing failed|artifact persistence failed|reconcile failed",
    ):
        execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert task.status == TASK_STATUS_FAILED
    assert run.status == EXECUTION_RUN_STATUS_FAILED
    assert validation_artifacts == []


def test_completed_execution_persists_exactly_one_validation_result_artifact(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Single validation artifact invariant",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    _patch_workspace_runtime(monkeypatch)
    _patch_execution_request(monkeypatch)
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=["context_selection_agent", "code_change_agent"],
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="completed",
        final_task_status=TASK_STATUS_COMPLETED,
    )

    execute_task_sync(db_session, task.id)

    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert len(validation_artifacts) == 1

    payload = json.loads(validation_artifacts[0].content)
    assert payload["execution_run_id"] == run.id
    assert payload["task_id"] == task.id
    assert payload["decision"] == "completed"


@pytest.mark.parametrize(
    "engine_decision",
    [
        EXECUTION_DECISION_FAILED,
        EXECUTION_DECISION_REJECTED,
    ],
)
def test_non_validable_terminal_outcomes_do_not_invoke_validation(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    engine_decision,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Terminal outcomes must bypass validation",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    expected_sequence = ["context_selection_agent", "router_agent"]

    _patch_workspace_runtime(monkeypatch)
    _patch_execution_request(monkeypatch)
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=engine_decision,
            execution_agent_sequence=expected_sequence,
        ),
    )

    def _boom(**kwargs):
        raise AssertionError("validate_execution_result should not be called")

    monkeypatch.setattr(
        "app.services.task_execution_service.validate_execution_result",
        _boom,
    )

    result = execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert result.validation_decision is None
    assert task.status == TASK_STATUS_FAILED
    assert validation_artifacts == []

    assert json.loads(task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(run.execution_agent_sequence) == expected_sequence


def test_artifact_persistence_failure_preserves_execution_agent_sequence_on_task_and_run(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Artifact persistence failure preserves agent trace",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    expected_sequence = [
        "context_selection_agent",
        "code_change_agent",
        "command_runner_agent",
    ]

    _patch_workspace_runtime(monkeypatch)
    _patch_execution_request(monkeypatch)
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=expected_sequence,
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="completed",
        final_task_status=TASK_STATUS_COMPLETED,
    )

    def _boom(**kwargs):
        raise RuntimeError("artifact persistence failed")

    monkeypatch.setattr(
        "app.services.task_execution_service._persist_validation_result_artifact",
        _boom,
    )

    with pytest.raises(
        TaskExecutionServiceError,
        match="post-validation processing failed|artifact persistence failed",
    ):
        execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)

    assert task.status == TASK_STATUS_FAILED
    assert run.status == EXECUTION_RUN_STATUS_FAILED

    assert json.loads(task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(run.execution_agent_sequence) == expected_sequence


@pytest.mark.parametrize(
    ("validation_decision", "final_task_status"),
    [
        ("completed", TASK_STATUS_COMPLETED),
        ("partial", TASK_STATUS_PARTIAL),
        ("failed", TASK_STATUS_FAILED),
        ("manual_review", TASK_STATUS_FAILED),
    ],
)
def test_validation_artifact_final_task_status_matches_persisted_task_status(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    validation_decision,
    final_task_status,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Validation artifact final status must match persisted task status",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    promoted = {"called": False}
    expected_sequence = ["context_selection_agent", "code_change_agent"]

    _patch_workspace_runtime(monkeypatch, promoted_state=promoted)
    _patch_execution_request(
        monkeypatch,
        allowed_paths=["app_service.py"],
        relevant_files=["app_service.py"],
    )
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=expected_sequence,
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision=validation_decision,
        final_task_status=final_task_status,
        followup_validation_required=(validation_decision == "partial"),
    )

    result = execute_task_sync(db_session, task.id)

    db_session.refresh(task)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert result.run_status == EXECUTION_RUN_STATUS_SUCCEEDED
    assert result.validation_decision == validation_decision
    assert result.final_task_status == final_task_status

    assert task.status == final_task_status
    assert run.status == EXECUTION_RUN_STATUS_SUCCEEDED

    assert json.loads(task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(run.execution_agent_sequence) == expected_sequence

    assert len(validation_artifacts) == 1

    payload = json.loads(validation_artifacts[0].content)
    assert payload["decision"] == validation_decision
    assert payload["final_task_status"] == task.status

    if validation_decision == "completed":
        assert promoted["called"] is True
        assert payload["workspace_promoted_to_source"] is True
    else:
        assert promoted["called"] is False
        assert payload["workspace_promoted_to_source"] is False


def test_validation_artifacts_remain_linked_to_the_correct_execution_run_across_multiple_runs(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Validation artifacts must stay attached to the correct run",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    first_sequence = ["context_selection_agent", "code_change_agent"]
    second_sequence = ["context_selection_agent", "code_change_agent", "command_runner_agent"]

    _patch_workspace_runtime(monkeypatch)
    _patch_execution_request(
        monkeypatch,
        allowed_paths=["app_service.py"],
        relevant_files=["app_service.py"],
    )

    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=first_sequence,
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="completed",
        final_task_status=TASK_STATUS_COMPLETED,
    )

    first_result = execute_task_sync(db_session, task.id)

    first_run = _get_latest_run_for_task(db_session, task.id)
    first_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert first_result.execution_run_id == first_run.id
    assert len(first_artifacts) == 1

    first_payload = json.loads(first_artifacts[0].content)
    assert first_payload["execution_run_id"] == first_run.id
    assert first_payload["task_id"] == task.id
    assert first_payload["decision"] == "completed"

    # Reopen the task explicitly to simulate a second valid execution cycle for the same task.
    task.status = "pending"
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_PARTIAL,
            execution_agent_sequence=second_sequence,
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="partial",
        final_task_status=TASK_STATUS_PARTIAL,
        followup_validation_required=True,
    )

    second_result = execute_task_sync(db_session, task.id)

    second_run = _get_latest_run_for_task(db_session, task.id)
    all_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert second_run.id != first_run.id
    assert second_result.execution_run_id == second_run.id
    assert len(all_artifacts) == 2

    payloads_by_run = {
        json.loads(artifact.content)["execution_run_id"]: json.loads(artifact.content)
        for artifact in all_artifacts
    }

    assert set(payloads_by_run.keys()) == {first_run.id, second_run.id}

    assert payloads_by_run[first_run.id]["decision"] == "completed"
    assert payloads_by_run[first_run.id]["final_task_status"] == TASK_STATUS_COMPLETED
    assert payloads_by_run[first_run.id]["task_id"] == task.id

    assert payloads_by_run[second_run.id]["decision"] == "partial"
    assert payloads_by_run[second_run.id]["final_task_status"] == TASK_STATUS_PARTIAL
    assert payloads_by_run[second_run.id]["task_id"] == task.id
    assert payloads_by_run[second_run.id]["followup_validation_required"] is True


def test_post_validation_failure_before_commit_does_not_leave_terminal_artifact_or_closed_task(
    db_session,
    monkeypatch,
    make_project,
    make_task,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Atomic closure must not persist partial state",
        status="pending",
        executor_type=EXECUTION_ENGINE,
    )

    expected_sequence = [
        "context_selection_agent",
        "code_change_agent",
        "command_runner_agent",
    ]

    _patch_workspace_runtime(monkeypatch)
    _patch_execution_request(
        monkeypatch,
        allowed_paths=["app_service.py"],
        relevant_files=["app_service.py"],
    )
    _patch_engine(
        monkeypatch,
        _build_engine_result(
            task_id=task.id,
            decision=EXECUTION_DECISION_COMPLETED,
            execution_agent_sequence=expected_sequence,
        ),
    )
    _patch_validation_service(
        monkeypatch,
        validation_decision="completed",
        final_task_status=TASK_STATUS_COMPLETED,
    )

    original_commit = db_session.commit
    state = {"commit_calls": 0}

    def fail_on_closure_commit():
        state["commit_calls"] += 1
        # 1: create_execution_run
        # 2: mark_execution_run_started + mark_task_running
        # 3: mark_execution_run_succeeded + agent sequence
        # 4: closure commit (artifact + task terminal status)
        if state["commit_calls"] == 4:
            raise RuntimeError("closure commit failed")
        return original_commit()

    monkeypatch.setattr(db_session, "commit", fail_on_closure_commit)

    with pytest.raises(
        TaskExecutionServiceError,
        match="post-validation processing failed|closure commit failed",
    ):
        execute_task_sync(db_session, task.id)

    db_session.expire_all()
    task = db_session.get(type(task), task.id)
    run = _get_latest_run_for_task(db_session, task.id)
    validation_artifacts = _get_validation_artifacts_for_task(db_session, task.id)

    assert task.status == TASK_STATUS_FAILED
    assert run.status == EXECUTION_RUN_STATUS_FAILED
    assert validation_artifacts == []

    assert json.loads(task.last_execution_agent_sequence) == expected_sequence
    assert json.loads(run.execution_agent_sequence) == expected_sequence
