"""
Unit tests for Phase 6 — enriched ErrorDiagnosis with fault_side and confidence.

Covers:
- ErrorDiagnosis schema has fault_side and confidence fields.
- _failed_verification_is_repairable_by_repo_changes uses fault_side:
    * "environment" → False (not repairable by repo changes)
    * "implementation" → True when changed_files present
    * "test" → True when changed_files present
    * "uncertain" → falls through to string-matching fallback
- _latest_step_failure_indicates_repairable_gap returns True for fault_side=environment
  when is_recoverable=True (environment_manager_agent path).
- _render_error_diagnosis_for_prompt includes fault_side and confidence.
- _build_completion_checklist: new_concrete_gap=True for fault_side=environment.
- changed_files argument flows through run_error_diagnosis (interface test).
"""

from __future__ import annotations

from app.execution_engine.contracts import (
    OBSERVATION_TYPE_ERROR_DIAGNOSIS,
    EvidenceItem,
    ExecutionEvidence,
    ExecutionRequest,
    HistoricalExecutionContext,
    ProjectExecutionContext,
)
from app.execution_engine.error_diagnosis import (
    DiagnosisConfidenceLiteral,
    ErrorDiagnosis,
    FaultSideLiteral,
)
from app.execution_engine.orchestrator import (
    ORCHESTRATOR_SYSTEM_PROMPT,
    _build_completion_checklist,
    _failed_verification_is_repairable_by_repo_changes,
    _latest_step_failure_indicates_repairable_gap,
    _render_error_diagnosis_for_prompt,
)
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.state import ExecutionState
from app.models.task import EXECUTION_ENGINE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    *,
    task_type: str = "implementation",
    verification_level: str = "runtime",
) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=1,
        execution_run_id=1,
        project_id=1,
        task_title="Implement user service",
        task_description="Write user_service.py",
        objective="User service functional.",
        acceptance_criteria="Passes all tests.",
        technical_constraints="Python 3.12",
        out_of_scope="Integration tests",
        task_type=task_type,
        verification_level=verification_level,
        executor_type=EXECUTION_ENGINE,
        context=ProjectExecutionContext(
            project_id=1,
            workspace_path="/tmp/ws",
            source_path="/tmp/src",
        ),
        historical_context=HistoricalExecutionContext(selected_task_runs=[]),
    )


def _make_state(request: ExecutionRequest) -> ResolutionState:
    return ResolutionState(execution_request=request)


def _make_runtime(*, visited: list[str] | None = None) -> ExecutionState:
    state = ExecutionState()
    state.agent_call_count = 2
    state.visited_agents = visited or []
    return state


class _FakeChangedFile:
    def __init__(self, path: str = "app/user_service.py"):
        self.path = path
        self.change_type = "modified"
        self.producer = "code_change_agent"


class _FakeCommand:
    def __init__(self, *, exit_code: int = 1, stdout: str = "", stderr: str = ""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = False
        self.expected_exit_codes = []
        self.command = "pytest tests/"
        self.observed_outcome_summary = stderr


def _add_diagnosis(
    evidence: ExecutionEvidence,
    *,
    fault_side: str = "implementation",
    confidence: str = "high",
    is_recoverable: bool = True,
    overall_error_class: str = "assertion_failure",
) -> None:
    evidence.observations.append(
        EvidenceItem(
            evidence_type=OBSERVATION_TYPE_ERROR_DIAGNOSIS,
            producer="command_runner_agent",
            summary="Error diagnosis",
            payload={
                "overall_error_class": overall_error_class,
                "fault_side": fault_side,
                "confidence": confidence,
                "is_recoverable": is_recoverable,
                "repair_directive": "Fix the assertion",
                "root_cause_errors": [],
                "cascade_errors": [],
                "cross_file_root_cause": None,
                "newly_discovered_files": [],
            },
        )
    )


# ---------------------------------------------------------------------------
# ErrorDiagnosis schema
# ---------------------------------------------------------------------------


def test_error_diagnosis_has_fault_side_and_confidence():
    """ErrorDiagnosis must expose fault_side and confidence typed fields."""
    d = ErrorDiagnosis(
        overall_error_class="assertion_failure",
        fault_side="implementation",
        confidence="high",
        repair_directive="Fix the assertion in user_service.py",
    )
    assert d.fault_side == "implementation"
    assert d.confidence == "high"


def test_error_diagnosis_fault_side_values():
    """All valid fault_side values must be accepted."""
    valid_fault_sides: list[FaultSideLiteral] = [
        "implementation",
        "test",
        "both",
        "environment",
        "uncertain",
    ]
    for side in valid_fault_sides:
        d = ErrorDiagnosis(
            overall_error_class="assertion_failure",
            fault_side=side,
            confidence="medium",
            repair_directive="Fix it",
        )
        assert d.fault_side == side


def test_error_diagnosis_confidence_values():
    """All valid confidence values must be accepted."""
    valid_confidences: list[DiagnosisConfidenceLiteral] = ["high", "medium", "low"]
    for conf in valid_confidences:
        d = ErrorDiagnosis(
            overall_error_class="runtime_error",
            fault_side="uncertain",
            confidence=conf,
            repair_directive="Check it",
        )
        assert d.confidence == conf


def test_error_diagnosis_is_recoverable_defaults_true():
    """is_recoverable defaults to True when not specified."""
    d = ErrorDiagnosis(
        overall_error_class="assertion_failure",
        fault_side="implementation",
        confidence="high",
        repair_directive="Fix it",
    )
    assert d.is_recoverable is True


# ---------------------------------------------------------------------------
# _failed_verification_is_repairable_by_repo_changes — fault_side routing
# ---------------------------------------------------------------------------


def test_repairable_false_for_environment_fault_side():
    """environment fault_side → False (not repairable by repo changes)."""
    request = _make_request()
    state = _make_state(request)
    state.evidence.changed_files.append(_FakeChangedFile())
    _add_diagnosis(state.evidence, fault_side="environment", is_recoverable=True)
    cmd = _FakeCommand(exit_code=1, stderr="ModuleNotFoundError: No module named 'pandas'")
    assert (
        _failed_verification_is_repairable_by_repo_changes(
            request=request, resolution_state=state, command=cmd
        )
        is False
    )


def test_repairable_true_for_implementation_fault_side():
    """implementation fault_side + changed_files → True."""
    request = _make_request()
    state = _make_state(request)
    state.evidence.changed_files.append(_FakeChangedFile())
    _add_diagnosis(state.evidence, fault_side="implementation", is_recoverable=True)
    cmd = _FakeCommand(exit_code=1, stderr="AssertionError")
    assert (
        _failed_verification_is_repairable_by_repo_changes(
            request=request, resolution_state=state, command=cmd
        )
        is True
    )


def test_repairable_true_for_test_fault_side():
    """test fault_side + changed_files → True (test_builder_agent path)."""
    request = _make_request()
    state = _make_state(request)
    state.evidence.changed_files.append(_FakeChangedFile())
    _add_diagnosis(state.evidence, fault_side="test", is_recoverable=True)
    cmd = _FakeCommand(exit_code=1, stderr="AssertionError: expected 200 got 404")
    assert (
        _failed_verification_is_repairable_by_repo_changes(
            request=request, resolution_state=state, command=cmd
        )
        is True
    )


def test_repairable_true_for_both_fault_side():
    """both fault_side + changed_files → True."""
    request = _make_request()
    state = _make_state(request)
    state.evidence.changed_files.append(_FakeChangedFile())
    _add_diagnosis(state.evidence, fault_side="both", is_recoverable=True)
    cmd = _FakeCommand(exit_code=1, stderr="AssertionError")
    assert (
        _failed_verification_is_repairable_by_repo_changes(
            request=request, resolution_state=state, command=cmd
        )
        is True
    )


def test_repairable_false_when_is_recoverable_false():
    """is_recoverable=False always returns False, regardless of fault_side."""
    request = _make_request()
    state = _make_state(request)
    state.evidence.changed_files.append(_FakeChangedFile())
    _add_diagnosis(state.evidence, fault_side="implementation", is_recoverable=False)
    cmd = _FakeCommand(exit_code=1, stderr="command not found: pytest")
    assert (
        _failed_verification_is_repairable_by_repo_changes(
            request=request, resolution_state=state, command=cmd
        )
        is False
    )


def test_repairable_false_for_implementation_without_changed_files():
    """implementation fault_side but no changed_files → False."""
    request = _make_request()
    state = _make_state(request)
    # No changed_files
    _add_diagnosis(state.evidence, fault_side="implementation", is_recoverable=True)
    cmd = _FakeCommand(exit_code=1, stderr="AssertionError")
    assert (
        _failed_verification_is_repairable_by_repo_changes(
            request=request, resolution_state=state, command=cmd
        )
        is False
    )


# ---------------------------------------------------------------------------
# _latest_step_failure_indicates_repairable_gap — environment path
# ---------------------------------------------------------------------------


def test_gap_true_for_environment_fault_side_recoverable():
    """
    When last subagent was command_runner_agent and diagnosis.fault_side='environment'
    with is_recoverable=True → new_concrete_gap=True (environment_manager_agent path).
    """
    request = _make_request()
    state = _make_state(request)
    state.failed_steps.append("dynamic_call_2_command_runner_agent")
    _add_diagnosis(state.evidence, fault_side="environment", is_recoverable=True)
    cmd = _FakeCommand(exit_code=1, stderr="No module named 'pandas'")
    state.evidence.commands.append(cmd)

    runtime = _make_runtime(visited=["context_selection_agent", "command_runner_agent"])
    assert _latest_step_failure_indicates_repairable_gap(request, state, runtime) is True


def test_gap_false_for_environment_fault_side_non_recoverable():
    """
    fault_side='environment' but is_recoverable=False → gap=False
    (the error can't even be fixed by environment_manager_agent).
    """
    request = _make_request()
    state = _make_state(request)
    state.failed_steps.append("dynamic_call_2_command_runner_agent")
    _add_diagnosis(state.evidence, fault_side="environment", is_recoverable=False)
    cmd = _FakeCommand(exit_code=1, stderr="network error")
    state.evidence.commands.append(cmd)

    runtime = _make_runtime(visited=["context_selection_agent", "command_runner_agent"])
    assert _latest_step_failure_indicates_repairable_gap(request, state, runtime) is False


def test_gap_true_when_env_manager_succeeded_and_command_runner_had_failed():
    """
    After env_manager SUCCEEDS (step not in failed_steps), last_attempted is
    'environment_manager_agent', but only command_runner step is in failed_steps.
    → gap=True (command_runner needs to re-verify the fixed environment).
    """
    request = _make_request()
    state = _make_state(request)
    # command_runner failed before env_manager ran
    state.failed_steps.append("dynamic_call_3_command_runner_agent")
    # env_manager itself succeeded (not in failed_steps)
    _add_diagnosis(state.evidence, fault_side="environment", is_recoverable=True)
    cmd = _FakeCommand(exit_code=1, stderr="No module named 'pandas'")
    state.evidence.commands.append(cmd)

    runtime = _make_runtime(
        visited=["context_selection_agent", "command_runner_agent", "environment_manager_agent"]
    )
    assert _latest_step_failure_indicates_repairable_gap(request, state, runtime) is True


def test_gap_false_when_env_manager_step_itself_failed():
    """
    When environment_manager_agent's own step appears in failed_steps,
    the failure is terminal → gap=False (no repair subagent can help).
    """
    request = _make_request()
    state = _make_state(request)
    # Both command_runner and env_manager failed
    state.failed_steps.append("dynamic_call_3_command_runner_agent")
    state.failed_steps.append("subagent_rejected_environment_manager_agent_4")
    _add_diagnosis(state.evidence, fault_side="environment", is_recoverable=True)
    cmd = _FakeCommand(exit_code=1, stderr="No module named 'pandas'")
    state.evidence.commands.append(cmd)

    runtime = _make_runtime(
        visited=["context_selection_agent", "command_runner_agent", "environment_manager_agent"]
    )
    assert _latest_step_failure_indicates_repairable_gap(request, state, runtime) is False


# ---------------------------------------------------------------------------
# _build_completion_checklist — environment failure path
# ---------------------------------------------------------------------------


def test_checklist_environment_failure_sets_gap_true():
    """
    When command_runner_agent fails with fault_side='environment' and is_recoverable=True,
    new_concrete_gap_detected must be True.
    """
    request = _make_request(task_type="implementation", verification_level="runtime")
    state = _make_state(request)
    state.completed_steps.append("dynamic_call_1_context_selection_agent")
    state.completed_steps.append("dynamic_call_2_code_change_agent")
    state.evidence.changed_files.append(_FakeChangedFile())

    cmd = _FakeCommand(exit_code=1, stderr="No module named 'pandas'")
    state.evidence.commands.append(cmd)
    _add_diagnosis(
        state.evidence,
        fault_side="environment",
        is_recoverable=True,
        overall_error_class="missing_dependency",
    )
    state.failed_steps.append("dynamic_call_3_command_runner_agent")

    runtime = _make_runtime(
        visited=["context_selection_agent", "code_change_agent", "command_runner_agent"]
    )

    checklist = _build_completion_checklist(request, state, runtime)

    assert checklist["context_ready"] is True
    assert checklist["implementation_done_if_needed"] is True
    assert checklist["local_verification_done_if_material"] is False
    assert checklist["new_concrete_gap_detected"] is True


def test_checklist_after_env_manager_success_gap_true():
    """
    After environment_manager_agent succeeds (installed the package), the checklist
    must still show new_concrete_gap_detected=True so the orchestrator re-runs
    command_runner_agent to verify the environment is now correct.
    """
    request = _make_request(task_type="implementation", verification_level="runtime")
    state = _make_state(request)
    state.completed_steps.append("dynamic_call_1_context_selection_agent")
    state.completed_steps.append("dynamic_call_2_code_change_agent")
    state.completed_steps.append("dynamic_call_4_environment_manager_agent")
    state.evidence.changed_files.append(_FakeChangedFile())

    cmd = _FakeCommand(exit_code=1, stderr="No module named 'pandas'")
    state.evidence.commands.append(cmd)
    _add_diagnosis(
        state.evidence,
        fault_side="environment",
        is_recoverable=True,
        overall_error_class="missing_dependency",
    )
    # command_runner failed (before env_manager ran); env_manager itself is NOT in failed_steps
    state.failed_steps.append("dynamic_call_3_command_runner_agent")

    runtime = _make_runtime(
        visited=[
            "context_selection_agent",
            "code_change_agent",
            "command_runner_agent",
            "environment_manager_agent",
        ]
    )

    checklist = _build_completion_checklist(request, state, runtime)

    assert checklist["new_concrete_gap_detected"] is True


def test_checklist_after_env_manager_failure_gap_false():
    """
    When environment_manager_agent itself fails (step in failed_steps), no subagent
    can repair the environment — new_concrete_gap_detected must be False.
    (In practice, Fix 4 causes immediate rejection before this path, but the
    function should be correct as defense-in-depth.)
    """
    request = _make_request(task_type="implementation", verification_level="runtime")
    state = _make_state(request)
    state.completed_steps.append("dynamic_call_1_context_selection_agent")
    state.completed_steps.append("dynamic_call_2_code_change_agent")
    state.evidence.changed_files.append(_FakeChangedFile())

    cmd = _FakeCommand(exit_code=1, stderr="No module named 'pandas'")
    state.evidence.commands.append(cmd)
    _add_diagnosis(
        state.evidence,
        fault_side="environment",
        is_recoverable=True,
        overall_error_class="missing_dependency",
    )
    state.failed_steps.append("dynamic_call_3_command_runner_agent")
    state.failed_steps.append("subagent_rejected_environment_manager_agent_4")

    runtime = _make_runtime(
        visited=[
            "context_selection_agent",
            "code_change_agent",
            "command_runner_agent",
            "environment_manager_agent",
        ]
    )

    checklist = _build_completion_checklist(request, state, runtime)

    assert checklist["new_concrete_gap_detected"] is False


# ---------------------------------------------------------------------------
# _render_error_diagnosis_for_prompt — includes fault_side and confidence
# ---------------------------------------------------------------------------


def test_render_includes_fault_side_and_confidence():
    """The rendered prompt string must include fault_side and confidence fields."""
    evidence = ExecutionEvidence()
    _add_diagnosis(evidence, fault_side="test", confidence="medium")
    rendered = _render_error_diagnosis_for_prompt(evidence)
    assert "fault_side: test" in rendered
    assert "confidence: medium" in rendered


def test_render_no_diagnosis_returns_placeholder():
    """No diagnosis in evidence → returns placeholder string."""
    evidence = ExecutionEvidence()
    rendered = _render_error_diagnosis_for_prompt(evidence)
    assert "none" in rendered.lower()


# ---------------------------------------------------------------------------
# ORCHESTRATOR_SYSTEM_PROMPT — Phase 6 content checks
# ---------------------------------------------------------------------------


def test_system_prompt_contains_environment_manager_agent():
    assert "environment_manager_agent" in ORCHESTRATOR_SYSTEM_PROMPT


def test_system_prompt_fault_side_routing():
    """The system prompt must document all fault_side values."""
    assert (
        '"implementation"' in ORCHESTRATOR_SYSTEM_PROMPT
        or "implementation" in ORCHESTRATOR_SYSTEM_PROMPT
    )
    assert "test" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "environment" in ORCHESTRATOR_SYSTEM_PROMPT


def test_system_prompt_missing_dependency_routes_to_env_manager():
    """'missing_dependency' must route to environment_manager_agent, not reject."""
    assert "environment_manager_agent" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "missing_dependency" in ORCHESTRATOR_SYSTEM_PROMPT


def test_system_prompt_confidence_mentioned():
    """The prompt must mention confidence to guide routing certainty."""
    assert "confidence" in ORCHESTRATOR_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Validation selection — environment_manager_agent must be ignored
# ---------------------------------------------------------------------------


def test_environment_manager_agent_is_in_ignored_validation_producers():
    """
    environment_manager_agent must be explicitly listed in IGNORED_VALIDATION_PRODUCERS
    so that its evidence notes are never passed to a validator.
    """
    from app.services.validation.selection import IGNORED_VALIDATION_PRODUCERS

    assert "environment_manager_agent" in IGNORED_VALIDATION_PRODUCERS


# ---------------------------------------------------------------------------
# ErrorDiagnosis backward compatibility
# ---------------------------------------------------------------------------


def test_error_diagnosis_fault_side_defaults_to_uncertain():
    """
    ErrorDiagnosis.fault_side defaults to 'uncertain' so that pre-Phase-6
    evidence payloads (without the field) can still be parsed via model_validate.
    """
    d = ErrorDiagnosis(
        overall_error_class="runtime_error",
        repair_directive="Check the error",
    )
    assert d.fault_side == "uncertain"


def test_error_diagnosis_confidence_defaults_to_medium():
    """
    ErrorDiagnosis.confidence defaults to 'medium' so that pre-Phase-6
    evidence payloads (without the field) can still be parsed via model_validate.
    """
    d = ErrorDiagnosis(
        overall_error_class="runtime_error",
        repair_directive="Check the error",
    )
    assert d.confidence == "medium"


def test_error_diagnosis_model_validate_without_fault_side_or_confidence():
    """
    model_validate on a dict without fault_side/confidence must succeed
    (backward compat for pre-Phase-6 stored evidence payloads).
    """
    payload = {
        "overall_error_class": "assertion_failure",
        "repair_directive": "Fix the assertion",
        "is_recoverable": True,
        "root_cause_errors": [],
        "cascade_errors": [],
        "cross_file_root_cause": None,
        "newly_discovered_files": [],
    }
    d = ErrorDiagnosis.model_validate(payload)
    assert d.fault_side == "uncertain"
    assert d.confidence == "medium"
