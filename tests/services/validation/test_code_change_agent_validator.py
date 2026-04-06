from __future__ import annotations

import pytest

from app.execution_engine.contracts import (
    CHANGE_TYPE_CREATED,
    CHANGE_TYPE_MODIFIED,
    ChangedFile,
    CommandExecution,
    ExecutionEvidence,
    ExecutionRequest,
    ExecutionResult,
    HistoricalExecutionContext,
    ProjectExecutionContext,
    RelatedTaskSummary,
)
from app.services.validation.contracts import TaskValidationInput
from app.services.validation.validators.code_change_agent_validator import (
    CodeChangeAgentValidator,
    CodeChangeAgentValidatorError,
)


class _CapturingProvider:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


def _make_execution_request(
    *,
    workspace_path: str,
    source_path: str,
    relevant_files: list[str] | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=10,
        project_id=20,
        execution_run_id=30,
        task_title="Implement user settings screen",
        task_description="Create the settings screen and wire the behavior.",
        task_summary="Mobile app vertical slice.",
        objective="Deliver a functional settings screen with the requested behavior.",
        proposed_solution="Implement the screen, related logic, and tests.",
        implementation_notes="Keep the existing structure and naming conventions.",
        implementation_steps="1. Add screen 2. Add tests 3. Verify behavior",
        acceptance_criteria="The settings screen exists and its critical behavior is covered.",
        tests_required="Relevant repository-local verification should pass.",
        technical_constraints="Do not break the current app structure.",
        out_of_scope="Do not change unrelated flows.",
        executor_type="execution_engine",
        success_criteria=["Screen exists", "Behavior works", "Tests pass"],
        constraints=["Stay within workspace"],
        allowed_paths=[
            "app/screens/settings.py",
            "tests/test_settings.py",
            "README.md",
        ],
        blocked_paths=[],
        context=ProjectExecutionContext(
            project_id=20,
            workspace_path=workspace_path,
            source_path=source_path,
            relevant_files=list(relevant_files or []),
            key_decisions=["Preserve navigation contract."],
            related_tasks=[
                RelatedTaskSummary(
                    task_id=999,
                    title="Related task",
                    status="pending",
                    summary="Downstream follow-up.",
                )
            ],
        ),
        historical_context=HistoricalExecutionContext(
            selected_task_runs=[],
        ),
    )


def _make_execution_result(
    *,
    changed_files: list[ChangedFile] | None = None,
    files_read=None,
    change_dependencies=None,
    commands: list[CommandExecution] | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        task_id=10,
        decision="partial",
        summary="Operational execution loop completed successfully.",
        details="Code and tests were materialized.",
        completed_scope="Created the settings screen and related files.",
        remaining_scope="External confirmation is still pending.",
        blockers_found=[],
        validation_notes=["Execution orchestrator finished normally."],
        output_snapshot="done",
        execution_agent_sequence=[
            "context_selection_agent",
            "code_change_agent",
            "command_runner_agent",
        ],
        evidence=ExecutionEvidence(
            changed_files=list(changed_files or []),
            files_read=list(files_read or []),
            change_dependencies=list(change_dependencies or []),
            commands=list(commands or []),
            notes=[],
            artifacts_created=[],
        ),
    )


def _make_validation_input(
    *,
    workspace_path: str,
    source_path: str,
    relevant_files: list[str] | None = None,
    changed_files: list[ChangedFile] | None = None,
    commands: list[CommandExecution] | None = None,
) -> TaskValidationInput:
    execution_request = _make_execution_request(
        workspace_path=workspace_path,
        source_path=source_path,
        relevant_files=relevant_files,
    )
    execution_result = _make_execution_result(
        changed_files=changed_files,
        commands=commands,
    )
    return TaskValidationInput(
        execution_request=execution_request,
        execution_result=execution_result,
        intent=None,
        metadata={},
    )


def _successful_llm_payload(*, decision: str = "completed") -> dict:
    return {
        "decision": decision,
        "summary": f"Validation concluded with decision={decision}.",
        "validated_scope": "The code contribution matches the task objective.",
        "missing_scope": None if decision == "completed" else "Some scope remains unresolved.",
        "blockers": [] if decision == "completed" else ["evidence gap"],
        "findings": [
            {
                "severity": "info" if decision == "completed" else "warning",
                "category": "objective_alignment",
                "message": "The validator assessed the code contribution against the task.",
                "evidence_refs": ["changed_file:0"],
                "file_paths": ["app/screens/settings.py"],
            }
        ],
        "manual_review_required": decision == "manual_review",
        "reasoning_notes": ["Grounded in task data and file contents."],
    }


def test_code_change_agent_validator_builds_prompt_with_context_and_evidence_files(
    tmp_path,
    monkeypatch,
):
    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()

    context_file = workspace_dir / "README.md"
    context_file.write_text(
        "# Project\n\nThis screen must preserve the current navigation contract.\n",
        encoding="utf-8",
    )

    changed_file = workspace_dir / "app" / "screens" / "settings.py"
    changed_file.parent.mkdir(parents=True, exist_ok=True)
    changed_file.write_text(
        "def render_settings_screen():\n    return 'settings-screen'\n",
        encoding="utf-8",
    )

    validation_input = _make_validation_input(
        workspace_path=str(workspace_dir),
        source_path=str(source_dir),
        relevant_files=["README.md"],
        changed_files=[
            ChangedFile(
                path="app/screens/settings.py",
                change_type=CHANGE_TYPE_CREATED,
                producer="code_change_agent",
            ),
        ],
        commands=[
            CommandExecution(
                command="pytest tests/test_settings.py -q",
                producer="command_runner_agent",
                exit_code=0,
                stdout="1 passed",
                stderr="",
            )
        ],
    )

    provider = _CapturingProvider(_successful_llm_payload())
    monkeypatch.setattr(
        "app.services.validation.validators.code_change_agent_validator.get_llm_provider",
        lambda: provider,
    )

    result = CodeChangeAgentValidator().validate(validation_input)

    assert result.decision == "completed"
    assert result.final_task_status == "completed"
    assert len(provider.calls) == 1

    user_prompt = provider.calls[0]["user_prompt"]

    assert "Subagent being validated:" in user_prompt
    assert "code_change_agent" in user_prompt
    assert (
        "Implements the task by deciding which repository files to create or modify" in user_prompt
    )

    assert "Context files to read and use:" in user_prompt
    assert "README.md" in user_prompt
    assert "preserve the current navigation contract" in user_prompt

    assert "Evidence-related files to read and use:" in user_prompt
    assert "app/screens/settings.py" in user_prompt
    assert "render_settings_screen" in user_prompt

    assert "Primary evidence to evaluate (producer = code_change_agent):" in user_prompt
    assert "changed_file" in user_prompt


def test_code_change_agent_validator_prefers_workspace_content_over_source_content(
    tmp_path,
    monkeypatch,
):
    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()

    workspace_file = workspace_dir / "app" / "screens" / "settings.py"
    source_file = source_dir / "app" / "screens" / "settings.py"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.parent.mkdir(parents=True, exist_ok=True)

    workspace_file.write_text(
        "WORKSPACE_VERSION = True\n",
        encoding="utf-8",
    )
    source_file.write_text(
        "SOURCE_VERSION = True\n",
        encoding="utf-8",
    )

    validation_input = _make_validation_input(
        workspace_path=str(workspace_dir),
        source_path=str(source_dir),
        relevant_files=[],
        changed_files=[
            ChangedFile(
                path="app/screens/settings.py",
                change_type=CHANGE_TYPE_MODIFIED,
                producer="code_change_agent",
            ),
        ],
    )

    provider = _CapturingProvider(_successful_llm_payload())
    monkeypatch.setattr(
        "app.services.validation.validators.code_change_agent_validator.get_llm_provider",
        lambda: provider,
    )

    CodeChangeAgentValidator().validate(validation_input)

    user_prompt = provider.calls[0]["user_prompt"]
    assert "WORKSPACE_VERSION = True" in user_prompt
    assert "SOURCE_VERSION = True" not in user_prompt


def test_code_change_agent_validator_validated_evidence_ids_include_only_code_change_agent_items(
    tmp_path,
    monkeypatch,
):
    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()

    changed_file = workspace_dir / "app" / "screens" / "settings.py"
    changed_file.parent.mkdir(parents=True, exist_ok=True)
    changed_file.write_text("print('settings')\n", encoding="utf-8")

    validation_input = _make_validation_input(
        workspace_path=str(workspace_dir),
        source_path=str(source_dir),
        relevant_files=[],
        changed_files=[
            ChangedFile(
                path="app/screens/settings.py",
                change_type=CHANGE_TYPE_MODIFIED,
                producer="code_change_agent",
            ),
            ChangedFile(
                path="tests/test_settings.py",
                change_type=CHANGE_TYPE_CREATED,
                producer="code_change_agent",
            ),
        ],
        commands=[
            CommandExecution(
                command="pytest tests/test_settings.py -q",
                producer="command_runner_agent",
                exit_code=0,
                stdout="2 passed",
                stderr="",
            )
        ],
    )

    provider = _CapturingProvider(_successful_llm_payload())
    monkeypatch.setattr(
        "app.services.validation.validators.code_change_agent_validator.get_llm_provider",
        lambda: provider,
    )

    result = CodeChangeAgentValidator().validate(validation_input)

    assert result.validated_evidence_ids == [
        "changed_file:0",
        "changed_file:1",
    ]
    assert result.unconsumed_evidence_ids == []


@pytest.mark.parametrize(
    ("decision", "expected_task_status", "expected_manual_review"),
    [
        ("completed", "completed", False),
        ("partial", "partial", False),
        ("failed", "failed", False),
        ("manual_review", "failed", True),
    ],
)
def test_code_change_agent_validator_maps_llm_decision_to_canonical_result(
    tmp_path,
    monkeypatch,
    decision,
    expected_task_status,
    expected_manual_review,
):
    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()

    changed_file = workspace_dir / "app" / "screens" / "settings.py"
    changed_file.parent.mkdir(parents=True, exist_ok=True)
    changed_file.write_text("print('settings')\n", encoding="utf-8")

    validation_input = _make_validation_input(
        workspace_path=str(workspace_dir),
        source_path=str(source_dir),
        relevant_files=[],
        changed_files=[
            ChangedFile(
                path="app/screens/settings.py",
                change_type=CHANGE_TYPE_MODIFIED,
                producer="code_change_agent",
            ),
        ],
    )

    provider = _CapturingProvider(_successful_llm_payload(decision=decision))
    monkeypatch.setattr(
        "app.services.validation.validators.code_change_agent_validator.get_llm_provider",
        lambda: provider,
    )

    result = CodeChangeAgentValidator().validate(validation_input)

    assert result.decision == decision
    assert result.final_task_status == expected_task_status
    assert result.manual_review_required is expected_manual_review


def test_code_change_agent_validator_includes_missing_files_in_prompt_without_crashing(
    tmp_path,
    monkeypatch,
):
    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()

    validation_input = _make_validation_input(
        workspace_path=str(workspace_dir),
        source_path=str(source_dir),
        relevant_files=["docs/spec.md"],
        changed_files=[
            ChangedFile(
                path="app/screens/settings.py",
                change_type=CHANGE_TYPE_CREATED,
                producer="code_change_agent",
            ),
        ],
    )

    provider = _CapturingProvider(_successful_llm_payload(decision="manual_review"))
    monkeypatch.setattr(
        "app.services.validation.validators.code_change_agent_validator.get_llm_provider",
        lambda: provider,
    )

    result = CodeChangeAgentValidator().validate(validation_input)

    assert result.decision == "manual_review"

    user_prompt = provider.calls[0]["user_prompt"]
    assert "docs/spec.md" in user_prompt
    assert "app/screens/settings.py" in user_prompt
    assert "resource_not_found" in user_prompt


def test_code_change_agent_validator_raises_on_invalid_llm_output(
    tmp_path,
    monkeypatch,
):
    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()

    changed_file = workspace_dir / "app" / "screens" / "settings.py"
    changed_file.parent.mkdir(parents=True, exist_ok=True)
    changed_file.write_text("print('settings')\n", encoding="utf-8")

    validation_input = _make_validation_input(
        workspace_path=str(workspace_dir),
        source_path=str(source_dir),
        relevant_files=[],
        changed_files=[
            ChangedFile(
                path="app/screens/settings.py",
                change_type=CHANGE_TYPE_MODIFIED,
                producer="code_change_agent",
            ),
        ],
    )

    provider = _CapturingProvider(
        {
            "decision": "completed",
            # falta "summary", así que debe romper la validación estructurada
        }
    )
    monkeypatch.setattr(
        "app.services.validation.validators.code_change_agent_validator.get_llm_provider",
        lambda: provider,
    )

    with pytest.raises(CodeChangeAgentValidatorError):
        CodeChangeAgentValidator().validate(validation_input)
