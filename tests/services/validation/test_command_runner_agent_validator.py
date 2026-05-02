from __future__ import annotations

import pytest

from app.execution_engine.contracts import (
    CHANGE_TYPE_CREATED,
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
from app.services.validation.validators.command_runner_agent_validator import (
    CommandRunnerAgentValidator,
    CommandRunnerAgentValidatorError,
    _task_looks_like_executable_implementation,
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
        task_title="Implement settings workflow",
        task_description="Create the settings workflow and verify it locally.",
        task_summary="Vertical slice with operational verification.",
        objective="Deliver a working settings workflow backed by repository-local verification.",
        proposed_solution="Implement code and verify the critical path.",
        implementation_notes="Stay within the existing app structure.",
        implementation_steps="1. Add code 2. Add tests 3. Run a narrow verification command",
        acceptance_criteria="The workflow exists and a relevant verification command passes.",
        tests_required="Repository-local verification should pass.",
        technical_constraints="Do not break unrelated flows.",
        out_of_scope="Do not modify unrelated features.",
        executor_type="execution_engine",
        success_criteria=["Workflow exists", "Verification command passes"],
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
            key_decisions=["Prefer one narrow repository-local verification step."],
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
    commands: list[CommandExecution] | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        task_id=10,
        decision="partial",
        summary="Operational execution loop completed successfully.",
        details="Repository-local verification evidence was produced.",
        completed_scope="Implemented the workflow and executed a verification step.",
        remaining_scope="External validation is still pending.",
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
            files_read=[],
            change_dependencies=[],
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
        "validated_scope": "The verification contribution was evaluated against the task objective.",
        "missing_scope": (
            None if decision == "completed" else "Verification confidence remains partial."
        ),
        "blockers": [] if decision == "completed" else ["verification gap"],
        "findings": [
            {
                "severity": "info" if decision == "completed" else "warning",
                "category": "verification_quality",
                "message": "The validator assessed the operational verification contribution.",
                "evidence_refs": ["command_execution:0"],
                "file_paths": [],
            }
        ],
        "manual_review_required": decision == "manual_review",
        "reasoning_notes": ["Grounded in task data, command evidence, and file contents."],
    }


def test_command_runner_agent_validator_builds_prompt_with_command_evidence_and_context(
    tmp_path,
    monkeypatch,
):
    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()

    context_file = workspace_dir / "README.md"
    context_file.write_text(
        "# Project\n\nUse one narrow repository-local verification step.\n",
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
                cwd="/tmp/run-tree",
                verification_goal="Verify settings workflow behavior.",
                rationale="Narrow verification over the changed workflow.",
                validation_claims=["The settings workflow is exercised by repository-local tests."],
                expected_exit_codes=[0],
                observed_outcome_summary="The targeted test passed.",
            )
        ],
    )

    provider = _CapturingProvider(_successful_llm_payload())
    monkeypatch.setattr(
        "app.services.validation.validators.command_runner_agent_validator.get_llm_provider",
        lambda: provider,
    )

    result = CommandRunnerAgentValidator().validate(validation_input)

    assert result.decision == "completed"
    assert result.final_task_status == "completed"
    assert len(provider.calls) == 1

    user_prompt = provider.calls[0]["user_prompt"]

    assert "Subagent being validated:" in user_prompt
    assert "command_runner_agent" in user_prompt
    assert "repository-local operational verification" in user_prompt
    assert "at most one narrow" in user_prompt

    assert "Primary evidence to evaluate (producer = command_runner_agent):" in user_prompt
    assert "pytest tests/test_settings.py -q" in user_prompt
    assert "Verify settings workflow behavior." in user_prompt
    assert "1 passed" in user_prompt
    assert "expected_exit_codes" in user_prompt

    assert "Command history summary:" in user_prompt
    assert "latest_command" in user_prompt
    assert "terminal_verification_clean: True" in user_prompt

    assert "Context files to read and use:" in user_prompt
    assert "README.md" in user_prompt
    assert "one narrow repository-local verification step" in user_prompt


def test_command_runner_agent_validator_validated_evidence_ids_include_only_command_runner_items(
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
            ),
            CommandExecution(
                command="python -m compileall app",
                producer="command_runner_agent",
                exit_code=0,
                stdout="ok",
                stderr="",
            ),
        ],
    )

    provider = _CapturingProvider(_successful_llm_payload())
    monkeypatch.setattr(
        "app.services.validation.validators.command_runner_agent_validator.get_llm_provider",
        lambda: provider,
    )

    result = CommandRunnerAgentValidator().validate(validation_input)

    assert result.validated_evidence_ids == [
        "command_execution:0",
        "command_execution:1",
    ]
    assert result.unconsumed_evidence_ids == []


@pytest.mark.parametrize(
    (
        "decision",
        "command_exit_code",
        "expected_result_decision",
        "expected_task_status",
        "expected_manual_review",
    ),
    [
        ("completed", 0, "completed", "completed", False),
        ("partial", 1, "partial", "partial", False),
        ("failed", 1, "failed", "failed", False),
        ("manual_review", 0, "manual_review", "failed", True),
    ],
)
def test_command_runner_agent_validator_maps_llm_decision_to_canonical_result(
    tmp_path,
    monkeypatch,
    decision,
    command_exit_code,
    expected_result_decision,
    expected_task_status,
    expected_manual_review,
):
    workspace_dir = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    workspace_dir.mkdir()
    source_dir.mkdir()

    validation_input = _make_validation_input(
        workspace_path=str(workspace_dir),
        source_path=str(source_dir),
        relevant_files=[],
        changed_files=[],
        commands=[
            CommandExecution(
                command="pytest tests/test_settings.py -q",
                producer="command_runner_agent",
                exit_code=command_exit_code,
                stdout="1 passed" if command_exit_code == 0 else "",
                stderr="" if command_exit_code == 0 else "1 failed",
                expected_exit_codes=[0],
                timed_out=False,
            ),
        ],
    )

    provider = _CapturingProvider(_successful_llm_payload(decision=decision))
    monkeypatch.setattr(
        "app.services.validation.validators.command_runner_agent_validator.get_llm_provider",
        lambda: provider,
    )

    result = CommandRunnerAgentValidator().validate(validation_input)

    assert result.decision == expected_result_decision
    assert result.final_task_status == expected_task_status
    assert result.manual_review_required is expected_manual_review


def test_command_runner_agent_validator_promotes_partial_to_completed_when_terminal_verification_is_clean(
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
        relevant_files=[],
        changed_files=[],
        commands=[
            CommandExecution(
                command="python -m unittest -v",
                producer="command_runner_agent",
                exit_code=5,
                stdout="",
                stderr="NO TESTS RAN",
                expected_exit_codes=[0],
                timed_out=False,
                verification_goal="Verify the workflow end to end.",
                rationale="Initial verification attempt.",
                validation_claims=["tests_discovered"],
                observed_outcome_summary="No tests were discovered.",
            ),
            CommandExecution(
                command='python -m unittest discover -s tests -p "test_*.py" -v',
                producer="command_runner_agent",
                exit_code=0,
                stdout="5 passed",
                stderr="",
                expected_exit_codes=[0],
                timed_out=False,
                verification_goal="Verify the workflow end to end.",
                rationale="Explicit discovery over the test suite.",
                validation_claims=["tests_passed"],
                observed_outcome_summary="The explicit discovery run succeeded.",
            ),
        ],
    )

    provider = _CapturingProvider(_successful_llm_payload(decision="partial"))
    monkeypatch.setattr(
        "app.services.validation.validators.command_runner_agent_validator.get_llm_provider",
        lambda: provider,
    )

    result = CommandRunnerAgentValidator().validate(validation_input)

    assert result.decision == "completed"
    assert result.final_task_status == "completed"
    assert result.manual_review_required is False
    assert result.metadata["terminal_verification_clean"] is True
    assert result.metadata["resolved_intermediate_failure"] is True


def test_command_runner_agent_validator_includes_missing_files_in_prompt_without_crashing(
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
        commands=[
            CommandExecution(
                command="pytest tests/test_settings.py -q",
                producer="command_runner_agent",
                exit_code=0,
                stdout="1 passed",
                stderr="",
            ),
        ],
    )

    provider = _CapturingProvider(_successful_llm_payload(decision="manual_review"))
    monkeypatch.setattr(
        "app.services.validation.validators.command_runner_agent_validator.get_llm_provider",
        lambda: provider,
    )

    result = CommandRunnerAgentValidator().validate(validation_input)

    assert result.decision == "manual_review"

    user_prompt = provider.calls[0]["user_prompt"]
    assert "docs/spec.md" in user_prompt
    assert "resource_not_found" in user_prompt


def test_command_runner_agent_validator_raises_on_invalid_llm_output(
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
        relevant_files=[],
        changed_files=[],
        commands=[
            CommandExecution(
                command="pytest tests/test_settings.py -q",
                producer="command_runner_agent",
                exit_code=0,
                stdout="1 passed",
                stderr="",
            ),
        ],
    )

    provider = _CapturingProvider(
        {
            "decision": "completed",
            # falta "summary"
        }
    )
    monkeypatch.setattr(
        "app.services.validation.validators.command_runner_agent_validator.get_llm_provider",
        lambda: provider,
    )

    with pytest.raises(CommandRunnerAgentValidatorError):
        CommandRunnerAgentValidator().validate(validation_input)


# ---------------------------------------------------------------------------
# D: _task_looks_like_executable_implementation markers
# ---------------------------------------------------------------------------


def _make_minimal_validation_input(*, title: str, description: str = "") -> TaskValidationInput:
    request = ExecutionRequest(
        task_id=1,
        project_id=1,
        execution_run_id=1,
        task_title=title,
        task_description=description,
        objective="",
        acceptance_criteria="",
        tests_required="",
        technical_constraints="",
        executor_type="execution_engine",
        context=ProjectExecutionContext(
            project_id=1,
            workspace_path="/tmp",
            source_path="/tmp",
        ),
    )
    result = ExecutionResult(
        task_id=1,
        decision="partial",
        summary="",
    )
    return TaskValidationInput(
        execution_request=request,
        execution_result=result,
        intent=None,
        metadata={},
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Removed broad markers — should no longer match
        ("List project requirements", False),
        ("Create documentation for the project", False),
        ("Write a specification document", False),
        ("Update the README", False),
        # Language-agnostic implementation markers — should match
        ("Implement the user endpoint", True),
        ("Add a new function to the auth module", True),
        ("Build the payment service", True),
        ("Refactor the database handler", True),
        ("Add unit test for the checkout component", True),
        ("Compile and verify the package", True),
        ("Add a new API route for user registration", True),
        # Cross-ecosystem coverage
        ("Implement the OrderController class", True),  # Java/Spring
        ("Build the Rust crate and run tests", True),  # Rust
        ("Add a React component for the dashboard", True),  # JS/TS
        ("Create a Go service for the auth module", True),  # service + module
        # "create" alone (no other marker) should not match
        ("Create a diagram for the onboarding flow", False),
    ],
)
def test_task_looks_like_executable_implementation(title, expected):
    validation_input = _make_minimal_validation_input(title=title)
    assert _task_looks_like_executable_implementation(validation_input) is expected
