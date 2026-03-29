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
from app.services.validation.contracts import ResolvedValidationIntent
from app.services.validation.evidence.package_builder import (
    build_task_validation_input,
)


def test_build_task_validation_input_collects_execution_context_and_evidence(
    tmp_path,
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        title="Implement service logic",
        description="Update service implementation and tests.",
        objective="Deliver the service behavior.",
        acceptance_criteria="Implementation and tests updated.",
        technical_constraints="Keep module structure stable.",
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

    (workspace_dir / "app_service.py").write_text(
        "def run():\n    return 'ok'\n",
        encoding="utf-8",
    )

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
            key_decisions=["Preserve the public interface."],
            related_tasks=[
                RelatedTaskSummary(
                    task_id=999,
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
        summary="Execution completed successfully.",
        details="Updated the service and supporting material.",
        completed_scope="Implemented the requested service behavior.",
        remaining_scope=None,
        blockers_found=[],
        validation_notes=["Execution completed without blockers."],
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
            artifacts_created=["artifact_id=42"],
        ),
    )

    intent = ResolvedValidationIntent(
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

    validation_input = build_task_validation_input(
        intent=intent,
        task=task,
        execution_request=execution_request,
        execution_result=execution_result,
        execution_run=execution_run,
        persisted_artifacts=[persisted_artifact],
    )

    assert validation_input.task.task_id == task.id
    assert validation_input.task.summary == task.summary
    assert validation_input.execution.execution_run_id == execution_run.id
    assert validation_input.execution.decision == "completed"
    assert validation_input.request_context.workspace_path == str(workspace_dir)
    assert validation_input.request_context.source_path == str(source_dir)
    assert validation_input.request_context.allowed_paths == ["app_service.py"]
    assert validation_input.request_context.related_task_ids == [999]

    evidence_by_id = {
        item.evidence_id: item
        for item in validation_input.evidence_package.evidence_items
    }

    assert "produced_file:app_service.py" in evidence_by_id
    produced_file = evidence_by_id["produced_file:app_service.py"]
    assert produced_file.evidence_kind == "produced_file"
    assert produced_file.path == "app_service.py"
    assert produced_file.change_type == CHANGE_TYPE_MODIFIED
    assert "def run()" in (produced_file.content_text or "")

    assert "command:0" in evidence_by_id
    command_item = evidence_by_id["command:0"]
    assert command_item.evidence_kind == "command_output"
    assert "pytest -q" in (command_item.content_text or "")

    assert "artifact:{}".format(persisted_artifact.id) in evidence_by_id
    persisted_item = evidence_by_id[f"artifact:{persisted_artifact.id}"]
    assert persisted_item.evidence_kind == "persisted_artifact"
    assert persisted_item.artifact_id == persisted_artifact.id

    assert "artifact_ref:0" in evidence_by_id
    artifact_ref_item = evidence_by_id["artifact_ref:0"]
    assert artifact_ref_item.evidence_kind == "artifact_reference"
    assert artifact_ref_item.content_summary == "artifact_id=42"

    assert validation_input.metadata["evidence_item_count"] == 4
    assert validation_input.metadata["produced_file_count"] == 1
    assert validation_input.metadata["command_count"] == 1
    assert validation_input.metadata["persisted_artifact_count"] == 1
