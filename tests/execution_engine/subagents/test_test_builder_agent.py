"""
Unit tests for TestBuilderAgent.

Covers:
- Successful file materialisation (create + modify)
- TestCoverageObservation observation produced after materialisation
- risk_flags populated when potential_implementation_gaps non-empty
- needs_dependency signal: observation emitted, files not written
- Invalid LLM output raises SubagentRejectedStepError
- Coverage assessment failure (None returned) does not roll back files
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.execution_engine.contracts import (
    CHANGE_TYPE_CREATED,
    OBSERVATION_TYPE_DEPENDENCY_REQUIRED,
    OBSERVATION_TYPE_TEST_COVERAGE,
    ExecutionRequest,
    HistoricalExecutionContext,
    ProjectExecutionContext,
)
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import SubagentRejectedStepError
from app.execution_engine.subagents.test_builder_agent import TestBuilderAgent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(workspace_path: str, source_path: str | None = None) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=1,
        project_id=2,
        execution_run_id=3,
        task_title="Write tests for UserService",
        task_description="Cover the acceptance criteria with pytest tests.",
        task_summary="Testing task.",
        objective="Have all acceptance criteria covered by tests.",
        acceptance_criteria="All CRUD operations on users have at least one passing test.",
        tests_required="pytest tests for create, read, update, delete operations.",
        technical_constraints="Python 3.12, pytest.",
        out_of_scope="Frontend tests.",
        executor_type="execution_engine",
        task_type="testing",
        context=ProjectExecutionContext(
            project_id=2,
            workspace_path=workspace_path,
            source_path=source_path or workspace_path,
            relevant_files=["app/services/user_service.py"],
            key_decisions=[],
            related_tasks=[],
        ),
        historical_context=HistoricalExecutionContext(selected_task_runs=[]),
    )


def _make_step() -> ExecutionStep:
    return ExecutionStep(
        id="dynamic_call_1_test_builder_agent",
        subagent_name="test_builder_agent",
        title="Write tests for UserService",
        instructions="Write tests covering the acceptance criteria.",
        target_paths=["tests/test_user_service.py"],
    )


def _make_state(request: ExecutionRequest) -> ResolutionState:
    state = ResolutionState(execution_request=request)
    state.phase = "execution"
    return state


class _CapturingRuntime:
    """Thin mock runtime that records generate_structured calls."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate_structured(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("No more responses configured")
        return self._responses.pop(0)


def _materialisation_payload(
    files: list[tuple[str, str, str]],  # (path, operation, content)
    *,
    warnings: list[str] | None = None,
    needs_dependency: str | None = None,
) -> dict:
    return {
        "summary": "Test files materialised.",
        "files": [
            {"path": p, "operation": op, "content": c, "rationale": "test"} for p, op, c in files
        ],
        "notes": [],
        "warnings": warnings or [],
        "needs_dependency": needs_dependency,
    }


def _coverage_payload(
    *,
    covered: list[str] | None = None,
    uncovered: list[str] | None = None,
    gaps: list[str] | None = None,
    tested_against: str = "acceptance_criteria",
    confidence: str = "high",
) -> dict:
    return {
        "covered_cases": covered or [],
        "uncovered_cases": uncovered or [],
        "potential_implementation_gaps": gaps or [],
        "tested_against": tested_against,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# File materialisation
# ---------------------------------------------------------------------------


def test_materialisation_creates_file(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload(
        [("tests/test_user_service.py", "create", "def test_create(): pass")]
    )
    runtime = _CapturingRuntime([payload, _coverage_payload(covered=["create user"])])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    written = tmp_path / "tests" / "test_user_service.py"
    assert written.exists()
    assert written.read_text() == "def test_create(): pass"

    changed_paths = [cf.path for cf in result.evidence.changed_files]
    assert "tests/test_user_service.py" in changed_paths


def test_materialisation_records_file_as_created(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload([("tests/test_user_service.py", "create", "pass")])
    runtime = _CapturingRuntime([payload, _coverage_payload()])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    changed_file = next(
        cf for cf in result.evidence.changed_files if cf.path == "tests/test_user_service.py"
    )
    assert changed_file.change_type == CHANGE_TYPE_CREATED
    assert changed_file.producer == "test_builder_agent"


def test_materialisation_modify_existing_file(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    (tmp_path / "tests").mkdir()
    existing = tmp_path / "tests" / "test_user_service.py"
    existing.write_text("def test_old(): pass")

    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload(
        [("tests/test_user_service.py", "modify", "def test_new(): pass")]
    )
    runtime = _CapturingRuntime([payload, _coverage_payload(covered=["create user"])])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    assert existing.read_text() == "def test_new(): pass"
    changed_paths = [cf.path for cf in result.evidence.changed_files]
    assert "tests/test_user_service.py" in changed_paths


def test_materialisation_increments_attempt_count(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload([("tests/test_user_service.py", "create", "pass")])
    runtime = _CapturingRuntime([payload, _coverage_payload()])

    agent = TestBuilderAgent(runtime=runtime)
    assert state.materialization_attempt_count == 0
    agent.execute_step(db=MagicMock(), request=request, step=step, state=state)
    assert state.materialization_attempt_count == 1


# ---------------------------------------------------------------------------
# TestCoverageObservation
# ---------------------------------------------------------------------------


def test_coverage_observation_added_to_evidence(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload([("tests/test_user_service.py", "create", "pass")])
    coverage = _coverage_payload(
        covered=["create user", "delete user"],
        uncovered=["update user"],
        confidence="medium",
    )
    runtime = _CapturingRuntime([payload, coverage])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    obs_items = [
        item
        for item in result.evidence.to_evidence_items()
        if item.evidence_type == OBSERVATION_TYPE_TEST_COVERAGE
    ]
    assert len(obs_items) == 1
    obs = obs_items[0]
    assert obs.producer == "test_builder_agent"
    assert obs.payload["confidence"] == "medium"
    assert "create user" in obs.payload["covered_cases"]
    assert "update user" in obs.payload["uncovered_cases"]


def test_coverage_observation_summary_contains_counts(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload([("tests/test_user_service.py", "create", "pass")])
    coverage = _coverage_payload(covered=["A", "B"], uncovered=["C"], confidence="high")
    runtime = _CapturingRuntime([payload, coverage])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    obs = next(
        item
        for item in result.evidence.to_evidence_items()
        if item.evidence_type == OBSERVATION_TYPE_TEST_COVERAGE
    )
    assert "2 covered" in obs.summary
    assert "1 uncovered" in obs.summary


# ---------------------------------------------------------------------------
# potential_implementation_gaps → risk_flags
# ---------------------------------------------------------------------------


def test_risk_flags_added_for_implementation_gaps(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload([("tests/test_user_service.py", "create", "pass")])
    coverage = _coverage_payload(
        covered=["create user"],
        gaps=["delete_user() is not implemented", "update_user() is missing"],
    )
    runtime = _CapturingRuntime([payload, coverage])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    flags = result.risk_flags
    assert any("delete_user() is not implemented" in f for f in flags)
    assert any("update_user() is missing" in f for f in flags)


def test_risk_flags_have_prefix(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload([("tests/test_user_service.py", "create", "pass")])
    coverage = _coverage_payload(gaps=["missing_feature"])
    runtime = _CapturingRuntime([payload, coverage])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    gap_flags = [f for f in result.risk_flags if f.startswith("potential_implementation_gap:")]
    assert len(gap_flags) == 1
    assert "missing_feature" in gap_flags[0]


def test_no_risk_flags_when_no_gaps(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload([("tests/test_user_service.py", "create", "pass")])
    coverage = _coverage_payload(covered=["create user"], gaps=[])
    runtime = _CapturingRuntime([payload, coverage])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    gap_flags = [f for f in result.risk_flags if f.startswith("potential_implementation_gap:")]
    assert gap_flags == []


# ---------------------------------------------------------------------------
# Coverage assessment failure does not roll back files
# ---------------------------------------------------------------------------


def test_coverage_assessment_failure_does_not_roll_back_files(tmp_path: Path) -> None:
    """If the coverage LLM call fails entirely, the materialised files must remain."""
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload([("tests/test_user_service.py", "create", "pass")])
    # Second call returns an invalid schema that cannot be validated (both retries fail).
    invalid_coverage = {"not_a_valid_field": "oops"}
    runtime = _CapturingRuntime([payload, invalid_coverage, invalid_coverage])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    # File must still exist
    assert (tmp_path / "tests" / "test_user_service.py").exists()
    # No coverage observation in evidence
    obs_items = [
        item
        for item in result.evidence.to_evidence_items()
        if item.evidence_type == OBSERVATION_TYPE_TEST_COVERAGE
    ]
    assert obs_items == []


# ---------------------------------------------------------------------------
# needs_dependency signal
# ---------------------------------------------------------------------------


def test_needs_dependency_emits_observation_and_no_files(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload(
        [],
        needs_dependency="pytest-asyncio>=0.23 — required for async tests",
    )
    runtime = _CapturingRuntime([payload])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    # No files written
    assert result.evidence.changed_files == []

    # Observation emitted
    obs_items = [
        item
        for item in result.evidence.to_evidence_items()
        if item.evidence_type == OBSERVATION_TYPE_DEPENDENCY_REQUIRED
    ]
    assert len(obs_items) == 1
    assert "pytest-asyncio" in obs_items[0].payload["package_description"]

    # needs_dependency signal on state
    assert state.needs_dependency_signal is not None
    assert "pytest-asyncio" in state.needs_dependency_signal


# ---------------------------------------------------------------------------
# Invalid LLM output
# ---------------------------------------------------------------------------


def test_invalid_llm_output_raises_subagent_rejected(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    bad_payload = {"totally_wrong": True}
    runtime = _CapturingRuntime([bad_payload])

    agent = TestBuilderAgent(runtime=runtime)
    with pytest.raises(SubagentRejectedStepError):
        agent.execute_step(db=MagicMock(), request=request, step=step, state=state)


def test_wrong_subagent_name_raises_subagent_rejected(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = ExecutionStep(
        id="dynamic_call_1_code_change_agent",
        subagent_name="code_change_agent",  # wrong subagent
        title="Write tests",
        instructions="...",
        target_paths=[],
    )
    state = _make_state(request)
    runtime = _CapturingRuntime([])

    agent = TestBuilderAgent(runtime=runtime)
    with pytest.raises(SubagentRejectedStepError, match="code_change_agent"):
        agent.execute_step(db=MagicMock(), request=request, step=step, state=state)


# ---------------------------------------------------------------------------
# Path boundary enforcement
# ---------------------------------------------------------------------------


def test_materialise_outside_workspace_raises(tmp_path: Path) -> None:
    workspace = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    # Path escaping the workspace via ../
    payload = _materialisation_payload([("../evil.py", "create", "malicious")])
    runtime = _CapturingRuntime([payload])

    agent = TestBuilderAgent(runtime=runtime)
    with pytest.raises(SubagentRejectedStepError, match="outside workspace"):
        agent.execute_step(db=MagicMock(), request=request, step=step, state=state)
