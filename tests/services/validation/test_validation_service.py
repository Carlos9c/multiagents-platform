import pytest

from app.execution_engine.contracts import (
    CHANGE_TYPE_MODIFIED,
    EXECUTION_DECISION_COMPLETED,
    ChangedFile,
    CommandExecution,
    ExecutionEvidence,
    ExecutionRequest,
    ExecutionResult,
    ProjectExecutionContext,
    RelatedTaskSummary,
)
from app.services.validation.contracts import ValidationResult
from app.services.validation.router.schemas import ValidationRoutingDecision
from app.services.validation.service import (
    ValidationServiceError,
    build_validation_routing_input,
    validate_execution_result,
)


def _make_code_routing_decision() -> ValidationRoutingDecision:
    return ValidationRoutingDecision(
        validator_key="code_task_validator",
        discipline="code",
        validation_mode="post_execution",
        requires_workspace=True,
        requires_file_reading=True,
        requires_changed_files=True,
        requires_command_results=True,
        requires_artifacts=True,
        requires_output_snapshot=True,
        requires_execution_agent_sequence=True,
        require_manual_review_if_evidence_missing=True,
        validation_focus=[
            "acceptance_criteria_alignment",
            "scope_completion",
            "repository_changes",
            "constraint_compliance",
        ],
        routing_rationale="Route code execution results to the code task validator.",
        open_questions=[],
    )


def test_build_validation_routing_input_collects_task_execution_and_evidence_summary(
    db_session,
    make_project,
    make_task,
    make_execution_run,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement service",
        description="Update service behavior.",
        objective="Deliver requested behavior.",
        acceptance_criteria="Service behavior is updated.",
        technical_constraints="Keep structure stable.",
        out_of_scope="No deployment changes.",
        executor_type="execution_engine",
        planning_level="atomic",
    )
    execution_run = make_execution_run(
        task_id=task.id,
        status="succeeded",
    )

    execution_request = ExecutionRequest(
        task_id=task.id,
        project_id=project.id,
        execution_run_id=execution_run.id,
        executor_type="execution_engine",
        task_title=task.title,
        task_description=task.description,
        task_summary=task.summary,
        objective=task.objective,
        acceptance_criteria=task.acceptance_criteria,
        technical_constraints=task.technical_constraints,
        out_of_scope=task.out_of_scope,
        allowed_paths=["app/service.py", "tests/test_service.py"],
        context=ProjectExecutionContext(
            project_id=project.id,
            workspace_path="/tmp/workspace",
            source_path="/tmp/source",
            relevant_files=["app/service.py"],
            key_decisions=["Preserve public interface."],
            related_tasks=[
                RelatedTaskSummary(
                    task_id=900,
                    title="Related task",
                    status="pending",
                    summary="Related downstream work.",
                )
            ],
        ),
    )

    execution_result = ExecutionResult(
        task_id=task.id,
        decision=EXECUTION_DECISION_COMPLETED,
        summary="Execution completed.",
        details="Service and tests updated.",
        completed_scope="Updated service behavior.",
        remaining_scope=None,
        blockers_found=[],
        validation_notes=["Execution finished normally."],
        output_snapshot="done",
        execution_agent_sequence=["planner", "editor", "tester"],
        evidence=ExecutionEvidence(
            changed_files=[
                ChangedFile(
                    path="app/service.py",
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
            artifacts_created=["artifact_id=123"],
        ),
    )

    routing_input = build_validation_routing_input(
        task=task,
        execution_request=execution_request,
        execution_result=execution_result,
        execution_run=execution_run,
    )

    assert routing_input.task.task_id == task.id
    assert routing_input.execution.execution_run_id == execution_run.id
    assert routing_input.execution.decision == "completed"
    assert routing_input.evidence.changed_file_paths == ["app/service.py"]
    assert routing_input.evidence.command_count == 1
    assert routing_input.evidence.artifact_refs == ["artifact_id=123"]
    assert routing_input.evidence.allowed_paths == [
        "app/service.py",
        "tests/test_service.py",
    ]
    assert routing_input.evidence.related_task_ids == [900]


def test_validate_execution_result_orchestrates_route_build_and_dispatch(
    tmp_path,
    monkeypatch,
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement service",
        description="Update service behavior.",
        objective="Deliver requested behavior.",
        acceptance_criteria="Service behavior is updated.",
        technical_constraints="Keep structure stable.",
        out_of_scope="No deployment changes.",
        executor_type="execution_engine",
        planning_level="atomic",
    )
    execution_run = make_execution_run(
        task_id=task.id,
        status="succeeded",
    )

    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()
    (workspace_dir / "app_service.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")

    persisted_artifact = make_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="code_validation_result",
        content='{"decision":"completed"}',
    )

    execution_request = ExecutionRequest(
        task_id=task.id,
        project_id=project.id,
        execution_run_id=execution_run.id,
        executor_type="execution_engine",
        task_title=task.title,
        task_description=task.description,
        task_summary=task.summary,
        objective=task.objective,
        acceptance_criteria=task.acceptance_criteria,
        technical_constraints=task.technical_constraints,
        out_of_scope=task.out_of_scope,
        allowed_paths=["app_service.py"],
        context=ProjectExecutionContext(
            project_id=project.id,
            workspace_path=str(workspace_dir),
            source_path=str(source_dir),
            relevant_files=[],
            key_decisions=["Preserve public interface."],
            related_tasks=[],
        ),
    )

    execution_result = ExecutionResult(
        task_id=task.id,
        decision=EXECUTION_DECISION_COMPLETED,
        summary="Execution completed.",
        details="Service updated.",
        completed_scope="Updated service behavior.",
        remaining_scope=None,
        blockers_found=[],
        validation_notes=["Execution finished normally."],
        output_snapshot="done",
        execution_agent_sequence=["planner", "editor", "tester"],
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
            artifacts_created=["artifact_id=123"],
        ),
    )

    captured = {}

    def fake_resolve_validation_route(*, routing_input):
        captured["routing_input"] = routing_input
        return _make_code_routing_decision()

    def fake_dispatch_validation(*, intent, validation_input):
        captured["intent"] = intent
        captured["validation_input"] = validation_input
        return ValidationResult(
            validator_key="code_task_validator",
            discipline="code",
            decision="completed",
            summary="Validation completed successfully.",
            validated_scope="Updated service behavior.",
            missing_scope=None,
            blockers=[],
            manual_review_required=False,
            final_task_status="completed",
            artifacts_created=[],
            validated_evidence_ids=["produced_file:app_service.py"],
            unconsumed_evidence_ids=[],
            followup_validation_required=False,
            recommended_next_validator_keys=[],
            partial_validation_summary=None,
            metadata={"confidence": "high"},
        )

    monkeypatch.setattr(
        "app.services.validation.service.resolve_validation_route",
        fake_resolve_validation_route,
    )
    monkeypatch.setattr(
        "app.services.validation.service.dispatch_validation",
        fake_dispatch_validation,
    )

    result = validate_execution_result(
        task=task,
        execution_request=execution_request,
        execution_result=execution_result,
        execution_run=execution_run,
        persisted_artifacts=[persisted_artifact],
    )

    assert result.routing_decision.validator_key == "code_task_validator"
    assert result.validation_result.decision == "completed"
    assert captured["routing_input"].task.task_id == task.id
    assert captured["validation_input"].task.task_id == task.id
    assert captured["validation_input"].evidence_package.evidence_items


def test_validate_execution_result_rejects_completed_with_failed_final_status(
    tmp_path,
    monkeypatch,
    db_session,
    make_project,
    make_task,
    make_execution_run,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement service",
        executor_type="execution_engine",
        planning_level="atomic",
    )
    execution_run = make_execution_run(
        task_id=task.id,
        status="succeeded",
    )

    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()
    (workspace_dir / "app_service.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")

    execution_request = ExecutionRequest(
        task_id=task.id,
        project_id=project.id,
        execution_run_id=execution_run.id,
        executor_type="execution_engine",
        task_title=task.title,
        task_description=task.description,
        task_summary=task.summary,
        objective=task.objective,
        acceptance_criteria=task.acceptance_criteria,
        technical_constraints=task.technical_constraints,
        out_of_scope=task.out_of_scope,
        allowed_paths=["app_service.py"],
        context=ProjectExecutionContext(
            project_id=project.id,
            workspace_path=str(workspace_dir),
            source_path=str(source_dir),
            relevant_files=[],
            key_decisions=[],
            related_tasks=[],
        ),
    )

    execution_result = ExecutionResult(
        task_id=task.id,
        decision=EXECUTION_DECISION_COMPLETED,
        summary="Execution completed.",
        details="Service updated.",
        completed_scope="Updated service behavior.",
        remaining_scope=None,
        blockers_found=[],
        validation_notes=["Execution finished normally."],
        output_snapshot="done",
        execution_agent_sequence=["planner", "editor"],
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
            notes=[],
            artifacts_created=[],
        ),
    )

    def fake_resolve_validation_route(*, routing_input):
        return _make_code_routing_decision()

    def fake_dispatch_validation(*, intent, validation_input):
        return ValidationResult(
            validator_key="code_task_validator",
            discipline="code",
            decision="completed",
            summary="Validation completed successfully.",
            validated_scope="Updated service behavior.",
            missing_scope=None,
            blockers=[],
            manual_review_required=False,
            final_task_status="failed",
            artifacts_created=[],
            validated_evidence_ids=["produced_file:app_service.py"],
            unconsumed_evidence_ids=[],
            followup_validation_required=False,
            recommended_next_validator_keys=[],
            partial_validation_summary=None,
            metadata={},
        )

    monkeypatch.setattr(
        "app.services.validation.service.resolve_validation_route",
        fake_resolve_validation_route,
    )
    monkeypatch.setattr(
        "app.services.validation.service.dispatch_validation",
        fake_dispatch_validation,
    )

    with pytest.raises(
        ValidationServiceError,
        match="decision='completed'.*final_task_status='completed'",
    ):
        validate_execution_result(
            task=task,
            execution_request=execution_request,
            execution_result=execution_result,
            execution_run=execution_run,
            persisted_artifacts=[],
        )


def test_validate_execution_result_rejects_completed_with_followup_validation_required(
    tmp_path,
    monkeypatch,
    db_session,
    make_project,
    make_task,
    make_execution_run,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement service",
        executor_type="execution_engine",
        planning_level="atomic",
    )
    execution_run = make_execution_run(task_id=task.id, status="succeeded")

    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()
    (workspace_dir / "app_service.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")

    execution_request = ExecutionRequest(
        task_id=task.id,
        project_id=project.id,
        execution_run_id=execution_run.id,
        executor_type="execution_engine",
        task_title=task.title,
        task_description=task.description,
        task_summary=task.summary,
        objective=task.objective,
        acceptance_criteria=task.acceptance_criteria,
        technical_constraints=task.technical_constraints,
        out_of_scope=task.out_of_scope,
        allowed_paths=["app_service.py"],
        context=ProjectExecutionContext(
            project_id=project.id,
            workspace_path=str(workspace_dir),
            source_path=str(source_dir),
            relevant_files=[],
            key_decisions=[],
            related_tasks=[],
        ),
    )

    execution_result = ExecutionResult(
        task_id=task.id,
        decision=EXECUTION_DECISION_COMPLETED,
        summary="Execution completed.",
        details="Service updated.",
        completed_scope="Updated service behavior.",
        remaining_scope=None,
        blockers_found=[],
        validation_notes=[],
        output_snapshot="done",
        execution_agent_sequence=["planner", "editor"],
        evidence=ExecutionEvidence(
            changed_files=[ChangedFile(path="app_service.py", change_type=CHANGE_TYPE_MODIFIED)],
            commands=[
                CommandExecution(
                    command="pytest -q",
                    exit_code=0,
                    stdout="1 passed",
                    stderr="",
                )
            ],
            notes=[],
            artifacts_created=[],
        ),
    )

    def fake_resolve_validation_route(*, routing_input):
        return _make_code_routing_decision()

    def fake_dispatch_validation(*, intent, validation_input):
        return ValidationResult(
            validator_key="code_task_validator",
            discipline="code",
            decision="completed",
            summary="Validation completed successfully.",
            validated_scope="Updated service behavior.",
            missing_scope=None,
            blockers=[],
            manual_review_required=False,
            final_task_status="completed",
            artifacts_created=[],
            validated_evidence_ids=["produced_file:app_service.py"],
            unconsumed_evidence_ids=[],
            followup_validation_required=True,
            recommended_next_validator_keys=[],
            partial_validation_summary=None,
            metadata={},
        )

    monkeypatch.setattr(
        "app.services.validation.service.resolve_validation_route",
        fake_resolve_validation_route,
    )
    monkeypatch.setattr(
        "app.services.validation.service.dispatch_validation",
        fake_dispatch_validation,
    )

    with pytest.raises(
        ValidationServiceError,
        match="decision='completed'.*follow-up validation",
    ):
        validate_execution_result(
            task=task,
            execution_request=execution_request,
            execution_result=execution_result,
            execution_run=execution_run,
            persisted_artifacts=[],
        )


def test_validate_execution_result_rejects_manual_review_without_manual_review_required(
    tmp_path,
    monkeypatch,
    db_session,
    make_project,
    make_task,
    make_execution_run,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement service",
        executor_type="execution_engine",
        planning_level="atomic",
    )
    execution_run = make_execution_run(task_id=task.id, status="succeeded")

    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()
    (workspace_dir / "app_service.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")

    execution_request = ExecutionRequest(
        task_id=task.id,
        project_id=project.id,
        execution_run_id=execution_run.id,
        executor_type="execution_engine",
        task_title=task.title,
        task_description=task.description,
        task_summary=task.summary,
        objective=task.objective,
        acceptance_criteria=task.acceptance_criteria,
        technical_constraints=task.technical_constraints,
        out_of_scope=task.out_of_scope,
        allowed_paths=["app_service.py"],
        context=ProjectExecutionContext(
            project_id=project.id,
            workspace_path=str(workspace_dir),
            source_path=str(source_dir),
            relevant_files=[],
            key_decisions=[],
            related_tasks=[],
        ),
    )

    execution_result = ExecutionResult(
        task_id=task.id,
        decision=EXECUTION_DECISION_COMPLETED,
        summary="Execution completed.",
        details="Service updated.",
        completed_scope="Updated service behavior.",
        remaining_scope=None,
        blockers_found=[],
        validation_notes=[],
        output_snapshot="done",
        execution_agent_sequence=["planner", "editor"],
        evidence=ExecutionEvidence(
            changed_files=[ChangedFile(path="app_service.py", change_type=CHANGE_TYPE_MODIFIED)],
            commands=[
                CommandExecution(
                    command="pytest -q",
                    exit_code=0,
                    stdout="1 passed",
                    stderr="",
                )
            ],
            notes=[],
            artifacts_created=[],
        ),
    )

    def fake_resolve_validation_route(*, routing_input):
        return _make_code_routing_decision()

    def fake_dispatch_validation(*, intent, validation_input):
        return ValidationResult(
            validator_key="code_task_validator",
            discipline="code",
            decision="manual_review",
            summary="Need manual review.",
            validated_scope=None,
            missing_scope="Validation uncertain.",
            blockers=[],
            manual_review_required=False,
            final_task_status="failed",
            artifacts_created=[],
            validated_evidence_ids=[],
            unconsumed_evidence_ids=[],
            followup_validation_required=False,
            recommended_next_validator_keys=[],
            partial_validation_summary=None,
            metadata={},
        )

    monkeypatch.setattr(
        "app.services.validation.service.resolve_validation_route",
        fake_resolve_validation_route,
    )
    monkeypatch.setattr(
        "app.services.validation.service.dispatch_validation",
        fake_dispatch_validation,
    )

    with pytest.raises(
        ValidationServiceError,
        match="manual_review.*manual_review_required=True",
    ):
        validate_execution_result(
            task=task,
            execution_request=execution_request,
            execution_result=execution_result,
            execution_run=execution_run,
            persisted_artifacts=[],
        )


def test_validate_execution_result_rejects_validator_key_mismatch(
    tmp_path,
    monkeypatch,
    db_session,
    make_project,
    make_task,
    make_execution_run,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement service",
        executor_type="execution_engine",
        planning_level="atomic",
    )
    execution_run = make_execution_run(task_id=task.id, status="succeeded")

    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()
    (workspace_dir / "app_service.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")

    execution_request = ExecutionRequest(
        task_id=task.id,
        project_id=project.id,
        execution_run_id=execution_run.id,
        executor_type="execution_engine",
        task_title=task.title,
        task_description=task.description,
        task_summary=task.summary,
        objective=task.objective,
        acceptance_criteria=task.acceptance_criteria,
        technical_constraints=task.technical_constraints,
        out_of_scope=task.out_of_scope,
        allowed_paths=["app_service.py"],
        context=ProjectExecutionContext(
            project_id=project.id,
            workspace_path=str(workspace_dir),
            source_path=str(source_dir),
            relevant_files=[],
            key_decisions=[],
            related_tasks=[],
        ),
    )

    execution_result = ExecutionResult(
        task_id=task.id,
        decision=EXECUTION_DECISION_COMPLETED,
        summary="Execution completed.",
        details="Service updated.",
        completed_scope="Updated service behavior.",
        remaining_scope=None,
        blockers_found=[],
        validation_notes=[],
        output_snapshot="done",
        execution_agent_sequence=["planner", "editor"],
        evidence=ExecutionEvidence(
            changed_files=[ChangedFile(path="app_service.py", change_type=CHANGE_TYPE_MODIFIED)],
            commands=[
                CommandExecution(
                    command="pytest -q",
                    exit_code=0,
                    stdout="1 passed",
                    stderr="",
                )
            ],
            notes=[],
            artifacts_created=[],
        ),
    )

    def fake_resolve_validation_route(*, routing_input):
        return _make_code_routing_decision()

    def fake_dispatch_validation(*, intent, validation_input):
        return ValidationResult(
            validator_key="other_validator",
            discipline="code",
            decision="completed",
            summary="Validation completed successfully.",
            validated_scope="Updated service behavior.",
            missing_scope=None,
            blockers=[],
            manual_review_required=False,
            final_task_status="completed",
            artifacts_created=[],
            validated_evidence_ids=["produced_file:app_service.py"],
            unconsumed_evidence_ids=[],
            followup_validation_required=False,
            recommended_next_validator_keys=[],
            partial_validation_summary=None,
            metadata={},
        )

    monkeypatch.setattr(
        "app.services.validation.service.resolve_validation_route",
        fake_resolve_validation_route,
    )
    monkeypatch.setattr(
        "app.services.validation.service.dispatch_validation",
        fake_dispatch_validation,
    )

    with pytest.raises(
        ValidationServiceError,
        match="validator_key does not match",
    ):
        validate_execution_result(
            task=task,
            execution_request=execution_request,
            execution_result=execution_result,
            execution_run=execution_run,
            persisted_artifacts=[],
        )
