"""
Unit tests for orchestrator functions related to test_builder_agent.

Covers:
- _allowed_subagents_for_phase includes test_builder_agent in execution phase
- _last_attempted_subagent_name recognises test_builder_agent
- _test_builder_agent_completed_a_step detects completed test_builder_agent steps
- _build_completion_checklist counts test_builder_agent completion as implementation done
- _maybe_build_forced_terminal_decision finishes after test_builder_agent with files
- next_action schema rejects unknown subagent names but accepts test_builder_agent
- VALID_SUBAGENT_NAMES contains test_builder_agent
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.execution_engine.budget import LoopBudget
from app.execution_engine.contracts import (
    ExecutionRequest,
    HistoricalExecutionContext,
    ProjectExecutionContext,
)
from app.execution_engine.next_action import VALID_SUBAGENT_NAMES, NextActionDecision
from app.execution_engine.orchestrator import (
    _allowed_subagents_for_phase,
    _build_completion_checklist,
    _last_attempted_subagent_name,
    _maybe_build_forced_terminal_decision,
    _task_explicitly_requests_repo_local_verification,
    _test_builder_agent_completed_a_step,
    _test_discovery_failure_in_latest_command,
    _verification_status_label,
    _verification_would_materially_improve,
)
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.state import ExecutionState
from app.models.task import EXECUTION_ENGINE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(*, task_type: str = "testing") -> ExecutionRequest:
    return ExecutionRequest(
        task_id=1,
        project_id=1,
        execution_run_id=1,
        task_title="Write tests for UserService",
        task_description="Cover acceptance criteria with pytest.",
        task_summary="Testing task.",
        objective="All acceptance criteria covered by tests.",
        acceptance_criteria="All CRUD operations have at least one passing test.",
        tests_required="pytest.",
        technical_constraints="Python 3.12.",
        out_of_scope="Frontend.",
        executor_type=EXECUTION_ENGINE,
        task_type=task_type,
        context=ProjectExecutionContext(
            project_id=1,
            workspace_path="/tmp/workspace",
            source_path="/tmp/source",
            relevant_files=[],
            key_decisions=[],
            related_tasks=[],
        ),
        historical_context=HistoricalExecutionContext(selected_task_runs=[]),
    )


def _make_state(request: ExecutionRequest, *, phase: str = "execution") -> ResolutionState:
    state = ResolutionState(execution_request=request)
    state.phase = phase
    return state


def _make_runtime_state(*, visited: list[str] | None = None) -> ExecutionState:
    rs = ExecutionState()
    for name in visited or []:
        rs.visited_agents.append(name)
        rs.agent_call_count += 1
    return rs


def _add_completed_step(state: ResolutionState, subagent_name: str, n: int = 1) -> None:
    state.completed_steps.append(f"dynamic_call_{n}_{subagent_name}")


def _add_changed_file(
    state: ResolutionState,
    path: str = "tests/test_user_service.py",
    producer: str = "test_builder_agent",
) -> None:
    state.evidence.add_changed_file(path=path, change_type="created", producer=producer)


def _add_successful_command(state: ResolutionState) -> None:
    state.evidence.add_command_execution(
        command="pytest tests/",
        producer="command_runner_agent",
        exit_code=0,
        stdout="1 passed",
        stderr="",
    )


def _add_failed_command_no_tests(state: ResolutionState) -> None:
    state.evidence.add_command_execution(
        command="pytest tests/",
        producer="command_runner_agent",
        exit_code=5,
        stdout="collected 0 items\n\nno tests ran",
        stderr="",
    )


# ---------------------------------------------------------------------------
# VALID_SUBAGENT_NAMES
# ---------------------------------------------------------------------------


def test_valid_subagent_names_includes_test_builder_agent():
    assert "test_builder_agent" in VALID_SUBAGENT_NAMES


# ---------------------------------------------------------------------------
# _allowed_subagents_for_phase
# ---------------------------------------------------------------------------


def test_allowed_subagents_execution_phase_includes_test_builder_agent():
    allowed = _allowed_subagents_for_phase("execution")
    assert "test_builder_agent" in allowed


def test_allowed_subagents_discovery_phase_excludes_test_builder_agent():
    allowed = _allowed_subagents_for_phase("discovery")
    assert "test_builder_agent" not in allowed


# ---------------------------------------------------------------------------
# _last_attempted_subagent_name
# ---------------------------------------------------------------------------


def test_last_attempted_subagent_name_recognises_test_builder_agent():
    rs = _make_runtime_state(visited=["context_selection_agent", "test_builder_agent"])
    assert _last_attempted_subagent_name(rs) == "test_builder_agent"


def test_last_attempted_subagent_name_test_builder_after_code_change():
    rs = _make_runtime_state(
        visited=["context_selection_agent", "code_change_agent", "test_builder_agent"]
    )
    assert _last_attempted_subagent_name(rs) == "test_builder_agent"


# ---------------------------------------------------------------------------
# _test_builder_agent_completed_a_step
# ---------------------------------------------------------------------------


def test_test_builder_agent_completed_a_step_true():
    request = _make_request()
    state = _make_state(request)
    _add_completed_step(state, "test_builder_agent")
    assert _test_builder_agent_completed_a_step(state) is True


def test_test_builder_agent_completed_a_step_false_when_code_change_only():
    request = _make_request()
    state = _make_state(request)
    _add_completed_step(state, "code_change_agent")
    assert _test_builder_agent_completed_a_step(state) is False


def test_test_builder_agent_completed_a_step_false_when_no_steps():
    request = _make_request()
    state = _make_state(request)
    assert _test_builder_agent_completed_a_step(state) is False


# ---------------------------------------------------------------------------
# _build_completion_checklist — test_builder_agent counts as implementation
# ---------------------------------------------------------------------------


def test_completion_checklist_implementation_done_after_test_builder():
    request = _make_request(task_type="testing")
    state = _make_state(request)
    _add_completed_step(state, "test_builder_agent")
    _add_changed_file(state)
    rs = _make_runtime_state(visited=["context_selection_agent", "test_builder_agent"])
    rs.agent_call_count = 2

    checklist = _build_completion_checklist(request, state, rs)

    assert checklist["implementation_done_if_needed"] is True


def test_completion_checklist_context_ready_after_context_selection():
    request = _make_request(task_type="testing")
    state = _make_state(request, phase="execution")
    _add_completed_step(state, "context_selection_agent", n=1)
    _add_completed_step(state, "test_builder_agent", n=2)
    _add_changed_file(state)
    rs = _make_runtime_state(visited=["context_selection_agent", "test_builder_agent"])
    rs.agent_call_count = 2

    checklist = _build_completion_checklist(request, state, rs)

    assert checklist["context_ready"] is True


# ---------------------------------------------------------------------------
# _maybe_build_forced_terminal_decision — finishes after test_builder_agent
# ---------------------------------------------------------------------------


def test_maybe_build_forced_finish_after_test_builder_agent_with_files():
    """
    test_builder_agent wrote files and verification_level=none means no further
    verification step is needed. _maybe_build_forced_terminal_decision should FINISH.
    """

    request = _make_request(task_type="testing")
    # Disable repo-local verification so _verification_would_materially_improve returns False.
    request = request.model_copy(update={"verification_level": "none"})

    state = _make_state(request)
    _add_completed_step(state, "test_builder_agent")
    _add_changed_file(state)

    rs = _make_runtime_state(visited=["context_selection_agent", "test_builder_agent"])
    rs.agent_call_count = 2

    budget = LoopBudget(
        max_steps=8,
        max_agent_calls=8,
        max_tool_calls=12,
        max_command_runs=4,
        max_repair_attempts=2,
    )

    decision = _maybe_build_forced_terminal_decision(
        request=request,
        resolution_state=state,
        runtime_state=rs,
        budget=budget,
        consecutive_invalid_decisions=0,
    )

    assert decision is not None
    assert decision.decision_type == "finish"


def test_maybe_build_does_not_force_finish_for_testing_task_before_command():
    """
    For task_type="testing" without verification_level=none, after test_builder_agent
    writes files, _verification_would_materially_improve is True so the orchestrator
    should NOT be forced to finish — command_runner_agent should still be called.
    """
    request = _make_request(task_type="testing")
    state = _make_state(request)
    _add_completed_step(state, "test_builder_agent")
    _add_changed_file(state)

    rs = _make_runtime_state(visited=["context_selection_agent", "test_builder_agent"])
    rs.agent_call_count = 2

    budget = LoopBudget(
        max_steps=8,
        max_agent_calls=8,
        max_tool_calls=12,
        max_command_runs=4,
        max_repair_attempts=2,
    )

    decision = _maybe_build_forced_terminal_decision(
        request=request,
        resolution_state=state,
        runtime_state=rs,
        budget=budget,
        consecutive_invalid_decisions=0,
    )

    # Should be None (let orchestrator decide to call command_runner_agent)
    assert decision is None


# ---------------------------------------------------------------------------
# Gap #1: forced FINISH must NOT fire for testing tasks before test_builder runs
# ---------------------------------------------------------------------------


def test_forced_finish_does_not_fire_for_testing_task_when_command_passes_before_test_builder():
    """
    Sequence: code_change_agent → command_runner_agent (pass).
    For task_type="testing", forced FINISH must NOT fire when test_builder_agent
    has not yet executed — even though command_runner succeeded.
    """
    request = _make_request(task_type="testing")
    state = _make_state(request)

    # code_change_agent ran and wrote a file
    _add_completed_step(state, "code_change_agent", n=2)
    _add_changed_file(state, path="app/services/user_service.py", producer="code_change_agent")

    # command_runner_agent succeeded (compile/type check)
    _add_completed_step(state, "command_runner_agent", n=3)
    _add_successful_command(state)

    rs = _make_runtime_state(
        visited=["context_selection_agent", "code_change_agent", "command_runner_agent"]
    )
    rs.agent_call_count = 3

    budget = LoopBudget(
        max_steps=8,
        max_agent_calls=8,
        max_tool_calls=12,
        max_command_runs=4,
        max_repair_attempts=2,
    )

    decision = _maybe_build_forced_terminal_decision(
        request=request,
        resolution_state=state,
        runtime_state=rs,
        budget=budget,
        consecutive_invalid_decisions=0,
    )

    # Must NOT force FINISH — test_builder_agent still needs to run
    assert decision is None


def test_forced_finish_fires_for_testing_task_after_test_builder_and_command():
    """
    Sequence: test_builder_agent → command_runner_agent (pass).
    After test_builder ran AND command_runner succeeded, forced FINISH should fire.
    """
    request = _make_request(task_type="testing")
    state = _make_state(request)

    _add_completed_step(state, "test_builder_agent", n=2)
    _add_changed_file(state)

    _add_completed_step(state, "command_runner_agent", n=3)
    _add_successful_command(state)

    rs = _make_runtime_state(
        visited=["context_selection_agent", "test_builder_agent", "command_runner_agent"]
    )
    rs.agent_call_count = 3

    budget = LoopBudget(
        max_steps=8,
        max_agent_calls=8,
        max_tool_calls=12,
        max_command_runs=4,
        max_repair_attempts=2,
    )

    decision = _maybe_build_forced_terminal_decision(
        request=request,
        resolution_state=state,
        runtime_state=rs,
        budget=budget,
        consecutive_invalid_decisions=0,
    )

    # test_builder ran AND command passed → forced FINISH is correct
    assert decision is not None
    assert decision.decision_type == "finish"


# ---------------------------------------------------------------------------
# Gap #3: _test_discovery_failure_in_latest_command
# ---------------------------------------------------------------------------


def test_test_discovery_failure_detected_on_zero_items_collected():
    request = _make_request()
    state = _make_state(request)
    _add_failed_command_no_tests(state)

    assert _test_discovery_failure_in_latest_command(state) is True


def test_test_discovery_failure_not_detected_on_assertion_failure():
    request = _make_request()
    state = _make_state(request)
    state.evidence.add_command_execution(
        command="pytest tests/",
        producer="command_runner_agent",
        exit_code=1,
        stdout="FAILED tests/test_user.py::test_create - AssertionError: assert 404 == 201",
        stderr="",
    )

    assert _test_discovery_failure_in_latest_command(state) is False


def test_test_discovery_failure_not_detected_when_no_commands():
    request = _make_request()
    state = _make_state(request)

    assert _test_discovery_failure_in_latest_command(state) is False


def test_test_discovery_failure_not_detected_when_command_succeeded():
    request = _make_request()
    state = _make_state(request)
    _add_successful_command(state)

    assert _test_discovery_failure_in_latest_command(state) is False


def test_operational_state_includes_test_discovery_failure_detected():
    """operational_state_summary must expose test_discovery_failure_detected."""
    from app.execution_engine.orchestrator import _build_operational_state_summary

    request = _make_request()
    state = _make_state(request)
    _add_failed_command_no_tests(state)
    rs = _make_runtime_state(visited=["context_selection_agent", "command_runner_agent"])
    rs.agent_call_count = 2

    summary = _build_operational_state_summary(request, state, rs)

    assert "test_discovery_failure_detected" in summary
    assert summary["test_discovery_failure_detected"] is True


# ---------------------------------------------------------------------------
# verification_level="deferred" — orchestrator behavior
# ---------------------------------------------------------------------------


def test_verification_would_not_improve_for_deferred_task():
    request = _make_request(task_type="implementation")
    request = request.model_copy(update={"verification_level": "deferred"})
    state = _make_state(request)
    _add_changed_file(state, path="app/service.py", producer="code_change_agent")

    assert _verification_would_materially_improve(request, state) is False


def test_explicit_verification_not_requested_for_deferred_task():
    request = _make_request(task_type="implementation")
    request = request.model_copy(update={"verification_level": "deferred"})

    assert _task_explicitly_requests_repo_local_verification(request) is False


def test_forced_finish_fires_after_code_change_on_deferred_task():
    """Deferred task: forced FINISH after code_change_agent — command_runner not needed."""
    request = _make_request(task_type="implementation")
    request = request.model_copy(update={"verification_level": "deferred"})

    state = _make_state(request)
    _add_completed_step(state, "code_change_agent", n=2)
    _add_changed_file(state, path="app/service.py", producer="code_change_agent")

    rs = _make_runtime_state(visited=["context_selection_agent", "code_change_agent"])
    rs.agent_call_count = 2

    budget = LoopBudget(
        max_steps=8,
        max_agent_calls=8,
        max_tool_calls=12,
        max_command_runs=4,
        max_repair_attempts=2,
    )
    decision = _maybe_build_forced_terminal_decision(
        request=request,
        resolution_state=state,
        runtime_state=rs,
        budget=budget,
        consecutive_invalid_decisions=0,
    )

    assert decision is not None
    assert decision.decision_type == "finish"


def test_verification_status_label_not_required_for_deferred():
    request = _make_request(task_type="implementation")
    request = request.model_copy(update={"verification_level": "deferred"})
    checklist = {"local_verification_done_if_material": False}

    assert _verification_status_label(request, checklist) == "not_required"


def test_verification_status_label_not_required_for_none():
    request = _make_request(task_type="implementation")
    request = request.model_copy(update={"verification_level": "none"})
    checklist = {"local_verification_done_if_material": False}

    assert _verification_status_label(request, checklist) == "not_required"


def test_verification_status_label_yes_when_runtime_and_done():
    request = _make_request(task_type="testing")
    # verification_level defaults to "runtime"
    checklist = {"local_verification_done_if_material": True}

    assert _verification_status_label(request, checklist) == "yes"


def test_verification_status_label_no_when_runtime_and_pending():
    request = _make_request(task_type="testing")
    checklist = {"local_verification_done_if_material": False}

    assert _verification_status_label(request, checklist) == "no"


# ---------------------------------------------------------------------------
# NextActionDecision schema — test_builder_agent is a valid subagent_name
# ---------------------------------------------------------------------------


def test_next_action_decision_accepts_test_builder_agent():
    d = NextActionDecision(
        decision_type="call_subagent",
        rationale="testing task requires test_builder_agent",
        subagent_name="test_builder_agent",
        target_paths=["tests/test_user_service.py"],
    )
    assert d.subagent_name == "test_builder_agent"


def test_next_action_decision_rejects_unknown_subagent():
    with pytest.raises(ValidationError):
        NextActionDecision(
            decision_type="call_subagent",
            rationale="unknown subagent",
            subagent_name="nonexistent_agent",
        )


# ---------------------------------------------------------------------------
# ValidationRegistry — test_builder_agent_validator registered
# ---------------------------------------------------------------------------


def test_validation_registry_has_test_builder_agent_validator():
    from app.services.validation.registry import ValidationRegistry

    registry = ValidationRegistry()
    assert registry.has_producer("test_builder_agent")
    v = registry.get_by_producer("test_builder_agent")
    assert v.validator_key == "test_builder_agent_validator"
    assert v.producer_key == "test_builder_agent"
