"""
Unit tests for Phase 5 — testing task routing and repair loop.

Covers:
- The orchestrator system prompt contains the new testing-task routing guidance.
- _verification_would_materially_improve returns True for a testing/runtime task
  after test_builder_agent ran but before command_runner_agent succeeds.
- _build_completion_checklist for the repair loop:
    test_builder_agent ran → command_runner_agent failed (repairable) →
    new_concrete_gap_detected=True, local_verification_done_if_material=False.
- Repair loop resolves correctly once command_runner_agent eventually succeeds.
- Non-repairable command_runner failure on a testing task → new_concrete_gap=False.
- Checklist guidance text in the orchestrator prompt covers the repair loop pattern.
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
from app.execution_engine.orchestrator import (
    ORCHESTRATOR_SYSTEM_PROMPT,
    _build_completion_checklist,
    _verification_would_materially_improve,
)
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.state import ExecutionState
from app.models.task import EXECUTION_ENGINE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    *,
    task_type: str = "testing",
    verification_level: str = "runtime",
    workspace_path: str = "/tmp/ws",
) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=1,
        execution_run_id=1,
        project_id=1,
        task_title="Write and verify user-service tests",
        task_description="Write unit tests for user_service.py and ensure they pass.",
        objective="All acceptance criteria covered by tests that execute successfully.",
        acceptance_criteria=(
            "Test file tests/test_user_service.py exists. "
            "All tests pass when executed with pytest."
        ),
        technical_constraints="pytest, Python 3.12",
        out_of_scope="Integration tests",
        task_type=task_type,
        verification_level=verification_level,
        executor_type=EXECUTION_ENGINE,
        context=ProjectExecutionContext(
            project_id=1,
            workspace_path=workspace_path,
            source_path="/tmp/src",
        ),
        historical_context=HistoricalExecutionContext(selected_task_runs=[]),
    )


def _make_state(request: ExecutionRequest) -> ResolutionState:
    return ResolutionState(execution_request=request)


def _make_runtime(*, agent_calls: int = 2, visited: list[str] | None = None) -> ExecutionState:
    state = ExecutionState()
    state.agent_call_count = agent_calls
    state.visited_agents = visited or []
    return state


class _FakeChangedFile:
    def __init__(self, path: str = "tests/test_user_service.py"):
        self.path = path
        self.change_type = "CREATED"
        self.producer = "test_builder_agent"


class _FakeCommand:
    def __init__(self, *, exit_code: int = 1, stdout: str = "", stderr: str = ""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = False
        self.expected_exit_codes = []
        self.command = "pytest tests/"
        self.observed_outcome_summary = stderr


def _add_error_diagnosis(evidence: ExecutionEvidence, *, is_recoverable: bool = True) -> None:
    evidence.observations.append(
        EvidenceItem(
            evidence_type=OBSERVATION_TYPE_ERROR_DIAGNOSIS,
            producer="command_runner_agent",
            summary="Error diagnosis",
            payload={
                "overall_error_class": "assertion_error",
                "is_recoverable": is_recoverable,
                "repair_directive": "Fix the assertion in user_service.py",
                "root_cause_errors": ["AssertionError: expected 200 got 404"],
                "cascade_errors": [],
                "cross_file_root_cause": None,
                "newly_discovered_files": [],
            },
        )
    )


def _set_test_builder_ran(state: ResolutionState) -> None:
    """Simulate test_builder_agent having completed a step."""
    state.completed_steps.append("dynamic_call_1_test_builder_agent")
    state.evidence.changed_files.append(_FakeChangedFile())


def _set_command_runner_failed(state: ResolutionState) -> None:
    """Simulate command_runner_agent having failed (added a command with exit_code=1)."""
    state.failed_steps.append("dynamic_call_2_command_runner_agent")


def _set_command_runner_succeeded(state: ResolutionState) -> None:
    """Simulate command_runner_agent having succeeded."""
    state.completed_steps.append("dynamic_call_2_command_runner_agent")
    cmd = _FakeCommand(exit_code=0, stdout="5 passed")
    state.evidence.commands.append(cmd)


# ---------------------------------------------------------------------------
# Orchestrator system prompt — testing task guidance present
# ---------------------------------------------------------------------------


def test_prompt_contains_acceptance_criteria_routing_guidance():
    """The system prompt must tell the LLM to route from acceptance_criteria, not file existence."""
    assert "acceptance_criteria" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "not from whether test files already exist" in ORCHESTRATOR_SYSTEM_PROMPT


def test_prompt_describes_repair_loop_pattern():
    """The system prompt must describe the full repair loop for testing tasks."""
    assert "test_builder_agent → command_runner_agent" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "code_change_agent" in ORCHESTRATOR_SYSTEM_PROMPT


def test_prompt_checklist_guidance_covers_runtime_verification():
    """The system prompt must tell the LLM that for runtime testing tasks,
    command_runner_agent must run after test_builder_agent."""
    # The execution-phase routing section contains this guidance
    assert 'verification_level="runtime"' in ORCHESTRATOR_SYSTEM_PROMPT
    assert "after test_builder_agent" in ORCHESTRATOR_SYSTEM_PROMPT


def test_prompt_checklist_guidance_covers_repairable_failure():
    """Checklist guidance must say repairable command_runner failure → new_concrete_gap=yes."""
    assert "repairable" in ORCHESTRATOR_SYSTEM_PROMPT
    # The guidance must also mention calling code_change_agent to repair
    assert "code_change_agent" in ORCHESTRATOR_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# _verification_would_materially_improve — testing task behaviour
# ---------------------------------------------------------------------------


def test_verification_needed_for_runtime_testing_task_before_any_command():
    """A testing/runtime task with no command run yet needs verification."""
    request = _make_request(task_type="testing", verification_level="runtime")
    state = _make_state(request)
    _set_test_builder_ran(state)
    # No command run yet
    assert _verification_would_materially_improve(request, state) is True


def test_verification_not_needed_when_command_succeeded():
    """Once command_runner_agent succeeded, no further verification is needed."""
    request = _make_request(task_type="testing", verification_level="runtime")
    state = _make_state(request)
    _set_test_builder_ran(state)
    _set_command_runner_succeeded(state)
    assert _verification_would_materially_improve(request, state) is False


def test_verification_needed_after_failed_command_on_testing_task():
    """After a failed command, re-verification is still needed (after the repair)."""
    request = _make_request(task_type="testing", verification_level="runtime")
    state = _make_state(request)
    _set_test_builder_ran(state)
    cmd = _FakeCommand(exit_code=1, stderr="AssertionError")
    state.evidence.commands.append(cmd)
    assert _verification_would_materially_improve(request, state) is True


def test_verification_not_needed_for_none_level_testing_task():
    """A testing task with verification_level='none' never needs command execution."""
    request = _make_request(task_type="testing", verification_level="none")
    state = _make_state(request)
    _set_test_builder_ran(state)
    assert _verification_would_materially_improve(request, state) is False


# ---------------------------------------------------------------------------
# _build_completion_checklist — repair loop scenarios
# ---------------------------------------------------------------------------


def test_checklist_repair_loop_entry_point():
    """
    Scenario: test_builder_agent ran (test files written), command_runner_agent failed
    with a repairable error (assertion_error, is_recoverable=True).

    Expected checklist:
    - implementation_done_if_needed = True  (test_builder ran, files changed)
    - local_verification_done_if_material = False  (command failed, not succeeded)
    - new_concrete_gap_detected = True  (repairable failure → code_change_agent needed)
    """
    request = _make_request(task_type="testing", verification_level="runtime")
    state = _make_state(request)
    _set_test_builder_ran(state)

    # command_runner_agent failed
    cmd = _FakeCommand(exit_code=1, stderr="AssertionError: expected 200 got 404")
    state.evidence.commands.append(cmd)
    _add_error_diagnosis(state.evidence, is_recoverable=True)
    _set_command_runner_failed(state)

    runtime = _make_runtime(
        visited=["context_selection_agent", "test_builder_agent", "command_runner_agent"]
    )

    checklist = _build_completion_checklist(request, state, runtime)

    assert checklist["context_ready"] is True
    assert checklist["implementation_done_if_needed"] is True  # test_builder ran
    assert checklist["local_verification_done_if_material"] is False  # command failed
    assert checklist["new_concrete_gap_detected"] is True  # repairable → gap exists


def test_checklist_after_successful_repair_and_reverification():
    """
    Scenario: full repair loop completed.
    test_builder_agent → command_runner_agent (failed) → code_change_agent → command_runner_agent (passed).

    Expected checklist: all satisfied, finish.
    """
    request = _make_request(task_type="testing", verification_level="runtime")
    state = _make_state(request)

    # test_builder_agent ran
    _set_test_builder_ran(state)
    # code_change_agent also ran (repair)
    state.completed_steps.append("dynamic_call_3_code_change_agent")
    from app.execution_engine.contracts import ChangedFile

    state.evidence.changed_files.append(
        ChangedFile(
            path="app/user_service.py", change_type="modified", producer="code_change_agent"
        )
    )
    # command_runner_agent eventually succeeded
    _set_command_runner_succeeded(state)

    runtime = _make_runtime(
        visited=[
            "context_selection_agent",
            "test_builder_agent",
            "command_runner_agent",
            "code_change_agent",
            "command_runner_agent",
        ]
    )

    checklist = _build_completion_checklist(request, state, runtime)

    assert checklist["context_ready"] is True
    assert checklist["implementation_done_if_needed"] is True
    assert checklist["local_verification_done_if_material"] is True
    assert checklist["new_concrete_gap_detected"] is False


def test_checklist_non_repairable_failure_no_new_gap():
    """
    Scenario: command_runner_agent failed with a non-repairable error
    (is_recoverable=False from error_diagnosis).

    new_concrete_gap_detected must be False — the loop should proceed to reject, not repair.
    """
    request = _make_request(task_type="testing", verification_level="runtime")
    state = _make_state(request)
    _set_test_builder_ran(state)

    cmd = _FakeCommand(exit_code=127, stderr="command not found: pytest")
    state.evidence.commands.append(cmd)
    _add_error_diagnosis(state.evidence, is_recoverable=False)
    _set_command_runner_failed(state)

    runtime = _make_runtime(
        visited=["context_selection_agent", "test_builder_agent", "command_runner_agent"]
    )

    checklist = _build_completion_checklist(request, state, runtime)

    assert checklist["local_verification_done_if_material"] is False
    assert checklist["new_concrete_gap_detected"] is False  # non-recoverable → no gap to repair


def test_checklist_test_builder_alone_does_not_finish_runtime_task():
    """
    Scenario: test_builder_agent ran successfully but command_runner_agent has not run yet.
    For a verification_level="runtime" task this is NOT complete.

    local_verification_done_if_material must be False.
    """
    request = _make_request(task_type="testing", verification_level="runtime")
    state = _make_state(request)
    _set_test_builder_ran(state)
    # No commands at all

    runtime = _make_runtime(visited=["context_selection_agent", "test_builder_agent"])

    checklist = _build_completion_checklist(request, state, runtime)

    assert checklist["implementation_done_if_needed"] is True
    assert checklist["local_verification_done_if_material"] is False
    # No failure either — new_concrete_gap is False (no failed step)
    assert checklist["new_concrete_gap_detected"] is False


def test_checklist_test_builder_alone_can_finish_none_level_task():
    """
    A testing task with verification_level="none" is complete once test_builder_agent
    has run — no command execution required.
    """
    request = _make_request(task_type="testing", verification_level="none")
    state = _make_state(request)
    _set_test_builder_ran(state)

    runtime = _make_runtime(visited=["context_selection_agent", "test_builder_agent"])

    checklist = _build_completion_checklist(request, state, runtime)

    assert checklist["implementation_done_if_needed"] is True
    assert checklist["local_verification_done_if_material"] is True
    assert checklist["new_concrete_gap_detected"] is False
    # All three satisfied, last is false → finish


def test_checklist_before_any_execution_step_context_not_ready():
    """Before context_selection_agent has run, context_ready is False."""
    request = _make_request()
    state = _make_state(request)
    # Phase is still "discovery", no completed steps
    runtime = _make_runtime(agent_calls=0, visited=[])

    checklist = _build_completion_checklist(request, state, runtime)

    assert checklist["context_ready"] is False
