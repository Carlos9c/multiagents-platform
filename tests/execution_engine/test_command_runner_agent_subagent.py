from __future__ import annotations

from typing import Any

import pytest

from app.execution_engine.contracts import (
    ExecutionEvidence,
    ExecutionRequest,
    ProjectExecutionContext,
)
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import SubagentRejectedStepError
from app.execution_engine.subagents.command_runner_agent import (
    CommandInspectionPlan,
    CommandRunnerAgent,
    _filter_valid_selected_paths,
)


class _FakeRuntime:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No more fake responses configured")
        return self._responses.pop(0)


def _make_request() -> ExecutionRequest:
    return ExecutionRequest(
        task_id=1,
        project_id=2,
        execution_run_id=3,
        task_title="Implement feature X",
        executor_type="execution_engine",
        context=ProjectExecutionContext(
            project_id=2,
            workspace_path="/tmp/workspace",
            source_path="/tmp/source",
        ),
    )


def _make_step() -> ExecutionStep:
    return ExecutionStep(
        id="step-1",
        subagent_name="command_runner_agent",
        title="Verify implementation",
        instructions="Run the verification command.",
    )


def _make_state() -> ResolutionState:
    return ResolutionState(
        execution_request=_make_request(),
        evidence=ExecutionEvidence(),
    )


def _make_agent(runtime: _FakeRuntime) -> CommandRunnerAgent:
    agent = CommandRunnerAgent.__new__(CommandRunnerAgent)
    agent.runtime = runtime
    agent.workspace_runtime = None
    return agent


_VALID_INSPECTION_RAW = {
    "selected_paths": [],
    "selection_rationale": "No files need inspection.",
    "verification_hypothesis": "Command planning will proceed using only inventory evidence.",
}

_VALID_COMMAND_PLAN_RAW = {
    "decision": "verification_not_applicable",
    "command": "",
    "cwd_relative_path": ".",
    "verification_goal": "No executable verification is applicable for this task.",
    "rationale": "The task does not produce executable output.",
    "validation_claims": [],
    "expected_exit_codes": [],
}

_INVALID_RAW: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# B: _filter_valid_selected_paths
# ---------------------------------------------------------------------------


def test_filter_keeps_all_valid_paths():
    inventory = ["a.py", "b.py", "c.py"]
    result = _filter_valid_selected_paths(selected_paths=["a.py", "c.py"], inventory=inventory)
    assert result == ["a.py", "c.py"]


def test_filter_drops_hallucinated_paths():
    inventory = ["a.py", "b.py"]
    result = _filter_valid_selected_paths(
        selected_paths=["a.py", "ghost.py", "invented/path.py"],
        inventory=inventory,
    )
    assert result == ["a.py"]


def test_filter_returns_empty_when_all_paths_are_invalid():
    result = _filter_valid_selected_paths(
        selected_paths=["ghost.py", "invented.py"],
        inventory=["real.py"],
    )
    assert result == []


def test_filter_returns_empty_for_empty_input():
    result = _filter_valid_selected_paths(selected_paths=[], inventory=["a.py"])
    assert result == []


def test_filter_preserves_order_of_valid_paths():
    inventory = {"a.py", "b.py", "c.py"}
    result = _filter_valid_selected_paths(
        selected_paths=["c.py", "a.py"],
        inventory=list(inventory),
    )
    assert result == ["c.py", "a.py"]


# ---------------------------------------------------------------------------
# C: retry in _select_files_for_inspection
# ---------------------------------------------------------------------------


def test_inspection_plan_succeeds_first_attempt(tmp_path):
    runtime = _FakeRuntime([_VALID_INSPECTION_RAW])
    agent = _make_agent(runtime)

    plan = agent._select_files_for_inspection(
        request=_make_request(),
        step=_make_step(),
        state=_make_state(),
        run_dir=tmp_path,
        inventory=[],
    )

    assert plan.selected_paths == []
    assert len(runtime.calls) == 1


def test_inspection_plan_retries_on_first_failure_and_succeeds(tmp_path):
    runtime = _FakeRuntime([_INVALID_RAW, _VALID_INSPECTION_RAW])
    agent = _make_agent(runtime)

    plan = agent._select_files_for_inspection(
        request=_make_request(),
        step=_make_step(),
        state=_make_state(),
        run_dir=tmp_path,
        inventory=[],
    )

    assert plan.selected_paths == []
    assert len(runtime.calls) == 2
    assert "Your previous output was invalid" in runtime.calls[1]["user_prompt"]


def test_inspection_plan_uses_fallback_when_both_attempts_fail(tmp_path):
    runtime = _FakeRuntime([_INVALID_RAW, _INVALID_RAW])
    agent = _make_agent(runtime)

    plan = agent._select_files_for_inspection(
        request=_make_request(),
        step=_make_step(),
        state=_make_state(),
        run_dir=tmp_path,
        inventory=[],
    )

    assert plan.selected_paths == []
    assert plan.selection_rationale
    assert plan.verification_hypothesis
    assert len(runtime.calls) == 2


def test_inspection_plan_filters_hallucinated_paths_from_valid_response(tmp_path):
    raw_with_ghost = {
        "selected_paths": ["real.py", "ghost.py"],
        "selection_rationale": "Select the relevant files.",
        "verification_hypothesis": "These files clarify the verification approach.",
    }
    runtime = _FakeRuntime([raw_with_ghost])
    agent = _make_agent(runtime)

    plan = agent._select_files_for_inspection(
        request=_make_request(),
        step=_make_step(),
        state=_make_state(),
        run_dir=tmp_path,
        inventory=["real.py"],
    )

    assert plan.selected_paths == ["real.py"]


# ---------------------------------------------------------------------------
# C: retry in _plan_command
# ---------------------------------------------------------------------------


def _make_inspection_plan() -> CommandInspectionPlan:
    return CommandInspectionPlan(
        selected_paths=[],
        selection_rationale="No files selected.",
        verification_hypothesis="Command planning proceeds without inspection.",
    )


def test_command_plan_succeeds_first_attempt(tmp_path):
    runtime = _FakeRuntime([_VALID_COMMAND_PLAN_RAW])
    agent = _make_agent(runtime)

    plan = agent._plan_command(
        request=_make_request(),
        step=_make_step(),
        state=_make_state(),
        run_dir=tmp_path,
        inventory=[],
        inspection_plan=_make_inspection_plan(),
        inspected_files=[],
    )

    assert plan.decision == "verification_not_applicable"
    assert len(runtime.calls) == 1


def test_command_plan_retries_on_first_failure_and_succeeds(tmp_path):
    runtime = _FakeRuntime([_INVALID_RAW, _VALID_COMMAND_PLAN_RAW])
    agent = _make_agent(runtime)

    plan = agent._plan_command(
        request=_make_request(),
        step=_make_step(),
        state=_make_state(),
        run_dir=tmp_path,
        inventory=[],
        inspection_plan=_make_inspection_plan(),
        inspected_files=[],
    )

    assert plan.decision == "verification_not_applicable"
    assert len(runtime.calls) == 2
    assert "Your previous output was invalid" in runtime.calls[1]["user_prompt"]


def test_command_plan_raises_after_both_attempts_fail(tmp_path):
    runtime = _FakeRuntime([_INVALID_RAW, _INVALID_RAW])
    agent = _make_agent(runtime)

    with pytest.raises(SubagentRejectedStepError, match="after retry"):
        agent._plan_command(
            request=_make_request(),
            step=_make_step(),
            state=_make_state(),
            run_dir=tmp_path,
            inventory=[],
            inspection_plan=_make_inspection_plan(),
            inspected_files=[],
        )

    assert len(runtime.calls) == 2
