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
    file_documentation: str = "Test file covering the task acceptance criteria.",
) -> dict:
    return {
        "summary": "Test files materialised.",
        "files": [
            {
                "path": p,
                "operation": op,
                "content": c,
                "rationale": "test",
                "file_documentation": file_documentation,
            }
            for p, op, c in files
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
    """Both primary and retry attempts fail → SubagentRejectedStepError raised."""
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    bad_payload = {"totally_wrong": True}
    # Primary call + retry both return invalid output
    runtime = _CapturingRuntime([bad_payload, bad_payload])

    agent = TestBuilderAgent(runtime=runtime)
    with pytest.raises(SubagentRejectedStepError):
        agent.execute_step(db=MagicMock(), request=request, step=step, state=state)


def test_primary_call_retries_once_on_invalid_output(tmp_path: Path) -> None:
    """Primary call fails ValidationError → retry is made → both calls consumed."""
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    bad_payload = {"totally_wrong": True}
    runtime = _CapturingRuntime([bad_payload, bad_payload])

    agent = TestBuilderAgent(runtime=runtime)
    with pytest.raises(SubagentRejectedStepError):
        agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    # Both primary and retry calls were consumed
    assert len(runtime.calls) == 2


def test_primary_call_succeeds_on_retry(tmp_path: Path) -> None:
    """Primary call fails but retry succeeds → agent continues normally."""
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    bad_payload = {"totally_wrong": True}
    good_payload = _materialisation_payload([("tests/test_user_service.py", "create", "pass")])
    # primary (invalid) + retry (valid) + coverage assessment
    runtime = _CapturingRuntime([bad_payload, good_payload, _coverage_payload(covered=["create"])])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    # Three LLM calls: primary fail + retry success + coverage
    assert len(runtime.calls) == 3
    changed_paths = [cf.path for cf in result.evidence.changed_files]
    assert "tests/test_user_service.py" in changed_paths


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


def test_infrastructure_files_written_before_test_files(tmp_path: Path) -> None:
    """conftest.py must be written before test files even if LLM returns them in reverse order."""
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    # LLM returns test file BEFORE conftest.py
    payload = {
        "summary": "Files materialised.",
        "files": [
            {
                "path": "tests/test_service.py",
                "operation": "create",
                "content": "def test_x(): pass",
                "rationale": "test",
                "file_documentation": "Tests for service.",
            },
            {
                "path": "tests/conftest.py",
                "operation": "create",
                "content": "import pytest",
                "rationale": "infra",
                "file_documentation": "Pytest conftest for path setup.",
            },
        ],
        "notes": [],
        "warnings": [],
        "needs_dependency": None,
    }
    runtime = _CapturingRuntime([payload, _coverage_payload()])

    agent = TestBuilderAgent(runtime=runtime)
    result = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    # Both files written
    assert (tmp_path / "tests" / "test_service.py").exists()
    assert (tmp_path / "tests" / "conftest.py").exists()

    # conftest.py must appear first in evidence (written first)
    changed_paths = [cf.path for cf in result.evidence.changed_files]
    conftest_idx = changed_paths.index("tests/conftest.py")
    service_idx = changed_paths.index("tests/test_service.py")
    assert conftest_idx < service_idx


def test_empty_file_documentation_raises_validation_error(tmp_path: Path) -> None:
    """file_documentation must be non-empty; an empty string raises SubagentRejectedStepError."""
    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = {
        "summary": "Files materialised.",
        "files": [
            {
                "path": "tests/test_user_service.py",
                "operation": "create",
                "content": "def test_x(): pass",
                "rationale": "test",
                "file_documentation": "",  # empty — should be rejected
            }
        ],
        "notes": [],
        "warnings": [],
        "needs_dependency": None,
    }
    runtime = _CapturingRuntime([payload])

    agent = TestBuilderAgent(runtime=runtime)
    with pytest.raises(SubagentRejectedStepError, match="file_documentation"):
        agent.execute_step(db=MagicMock(), request=request, step=step, state=state)


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


# ---------------------------------------------------------------------------
# _paths_to_tree
# ---------------------------------------------------------------------------


from app.execution_engine.file_operations import MaterializedFile  # noqa: E402
from app.execution_engine.subagents.test_builder_agent import (  # noqa: E402
    _build_current_run_code_changes_context,
    _build_last_command_failure_summary,
    _build_related_file_context,
    _paths_to_tree,
    _sort_files_infrastructure_first,
)


def test_paths_to_tree_empty_returns_empty_string() -> None:
    assert _paths_to_tree([]) == ""


def test_paths_to_tree_single_file() -> None:
    result = _paths_to_tree(["tests/test_foo.py"])
    assert "tests" in result
    assert "test_foo.py" in result


def test_paths_to_tree_nested_structure() -> None:
    paths = [
        "src/auth/service.py",
        "src/auth/__init__.py",
        "tests/test_auth.py",
    ]
    result = _paths_to_tree(paths)
    assert "src" in result
    assert "auth" in result
    assert "service.py" in result
    assert "tests" in result
    assert "test_auth.py" in result


def test_paths_to_tree_preserves_all_files() -> None:
    paths = ["a/b/c.py", "a/d.py", "e.py"]
    result = _paths_to_tree(paths)
    assert "c.py" in result
    assert "d.py" in result
    assert "e.py" in result


def test_paths_to_tree_uses_tree_connectors() -> None:
    paths = ["foo/bar.py", "foo/baz.py"]
    result = _paths_to_tree(paths)
    # Tree rendering uses box-drawing connectors
    assert "├──" in result or "└──" in result


# ---------------------------------------------------------------------------
# Execution trace written after materialisation
# ---------------------------------------------------------------------------


def test_execution_trace_written_after_materialisation(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload([("tests/test_user_service.py", "create", "pass")])
    coverage = _coverage_payload(covered=["create user"], confidence="high")
    runtime = _CapturingRuntime([payload, coverage])

    captured: list[dict] = []

    def _fake_append(*, project_id, entry):
        captured.append(entry)

    with patch(
        "app.execution_engine.subagents.test_builder_agent.append_execution_trace",
        side_effect=_fake_append,
    ):
        agent = TestBuilderAgent(runtime=runtime)
        agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    assert len(captured) == 1
    entry = captured[0]
    assert entry["agent"] == "test_builder_agent"
    assert entry["task_id"] == request.task_id
    assert entry["run_id"] == request.execution_run_id
    assert entry["call_type"] == "materialise"
    assert any(f["path"] == "tests/test_user_service.py" for f in entry["files_written"])
    assert entry["coverage_summary"]["confidence"] == "high"
    assert entry["coverage_summary"]["covered_count"] == 1


# ---------------------------------------------------------------------------
# _sort_files_infrastructure_first
# ---------------------------------------------------------------------------


def test_infrastructure_files_sorted_before_test_files() -> None:
    files = [
        MaterializedFile(
            path="tests/test_service.py",
            operation="create",
            content="",
            rationale="",
            file_documentation="d",
        ),
        MaterializedFile(
            path="conftest.py", operation="create", content="", rationale="", file_documentation="d"
        ),
        MaterializedFile(
            path="tests/test_auth.py",
            operation="create",
            content="",
            rationale="",
            file_documentation="d",
        ),
    ]
    ordered = _sort_files_infrastructure_first(files)
    assert ordered[0].path == "conftest.py"
    test_paths = {f.path for f in ordered[1:]}
    assert "tests/test_service.py" in test_paths
    assert "tests/test_auth.py" in test_paths


def test_nested_conftest_sorted_before_sibling_test_files() -> None:
    """tests/conftest.py must come before tests/accounts/test_endpoint.py."""
    files = [
        MaterializedFile(
            path="tests/accounts/test_endpoint.py",
            operation="create",
            content="",
            rationale="",
            file_documentation="d",
        ),
        MaterializedFile(
            path="tests/conftest.py",
            operation="create",
            content="",
            rationale="",
            file_documentation="d",
        ),
    ]
    ordered = _sort_files_infrastructure_first(files)
    assert ordered[0].path == "tests/conftest.py"


def test_jest_config_sorted_before_test_files() -> None:
    files = [
        MaterializedFile(
            path="src/__tests__/service.test.ts",
            operation="create",
            content="",
            rationale="",
            file_documentation="d",
        ),
        MaterializedFile(
            path="jest.config.ts",
            operation="create",
            content="",
            rationale="",
            file_documentation="d",
        ),
    ]
    ordered = _sort_files_infrastructure_first(files)
    assert ordered[0].path == "jest.config.ts"


def test_non_infrastructure_files_sorted_alphabetically_among_themselves() -> None:
    files = [
        MaterializedFile(
            path="tests/test_z.py",
            operation="create",
            content="",
            rationale="",
            file_documentation="d",
        ),
        MaterializedFile(
            path="tests/test_a.py",
            operation="create",
            content="",
            rationale="",
            file_documentation="d",
        ),
    ]
    ordered = _sort_files_infrastructure_first(files)
    assert ordered[0].path == "tests/test_a.py"
    assert ordered[1].path == "tests/test_z.py"


# ---------------------------------------------------------------------------
# _build_related_file_context — relevant_files bypass max_files cap
# ---------------------------------------------------------------------------


def test_relevant_files_bypass_max_files_cap(tmp_path: Path) -> None:
    """relevant_files must be loaded even when max_files is 0 (the cap only limits historical)."""
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    # Create 3 relevant files in source
    for i in range(3):
        f = source / f"module_{i}.py"
        f.write_text(f"# module {i}")

    request = ExecutionRequest(
        task_id=1,
        project_id=2,
        execution_run_id=3,
        task_title="Test",
        task_description=None,
        task_summary=None,
        objective=None,
        acceptance_criteria=None,
        tests_required=None,
        technical_constraints=None,
        out_of_scope=None,
        executor_type="execution_engine",
        task_type="testing",
        context=ProjectExecutionContext(
            project_id=2,
            workspace_path=str(workspace),
            source_path=str(source),
            relevant_files=["module_0.py", "module_1.py", "module_2.py"],
            key_decisions=[],
            related_tasks=[],
        ),
        historical_context=HistoricalExecutionContext(selected_task_runs=[]),
    )

    context_str, files_read = _build_related_file_context(
        workspace_root=str(workspace),
        source_root=str(source),
        request=request,
        max_files=0,  # historical cap is 0 — relevant_files should still load
    )

    # All 3 relevant files must be present
    assert "module_0.py" in context_str
    assert "module_1.py" in context_str
    assert "module_2.py" in context_str
    assert len(files_read) == 3


# ---------------------------------------------------------------------------
# Gap #2: _build_current_run_code_changes_context
# ---------------------------------------------------------------------------


def test_code_change_agent_files_exposed_in_context(tmp_path: Path) -> None:
    """Files written by code_change_agent in this run must appear in the context."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app").mkdir()
    (workspace / "app" / "service.py").write_text("def create_user(name): return {'id': 1}")

    request = _make_request(str(workspace))
    state = _make_state(request)
    state.evidence.add_changed_file(
        path="app/service.py",
        change_type="created",
        producer="code_change_agent",
    )

    context_str, files_read = _build_current_run_code_changes_context(
        workspace_root=str(workspace),
        state=state,
    )

    assert "app/service.py" in context_str
    assert "create_user" in context_str
    assert any("app/service.py" == path for path, _ in files_read)


def test_code_change_agent_context_empty_when_no_code_agent_files(tmp_path: Path) -> None:
    """When only test_builder_agent wrote files, the code context is empty."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    request = _make_request(str(workspace))
    state = _make_state(request)
    state.evidence.add_changed_file(
        path="tests/test_service.py",
        change_type="created",
        producer="test_builder_agent",  # not code_change_agent
    )

    context_str, files_read = _build_current_run_code_changes_context(
        workspace_root=str(workspace),
        state=state,
    )

    assert "no implementation files written by code_change_agent" in context_str
    assert files_read == []


def test_code_change_context_included_in_primary_prompt(tmp_path: Path) -> None:
    """code_change_agent files must appear in the user prompt sent to the LLM."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app").mkdir()
    (workspace / "app" / "service.py").write_text("def create_user(name): ...")

    request = _make_request(str(workspace))
    step = _make_step()
    state = _make_state(request)
    state.evidence.add_changed_file(
        path="app/service.py",
        change_type="created",
        producer="code_change_agent",
    )

    good_payload = _materialisation_payload([("tests/test_service.py", "create", "pass")])
    runtime = _CapturingRuntime([good_payload, _coverage_payload()])

    agent = TestBuilderAgent(runtime=runtime)
    agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    # The primary call (index 0) must contain the code_change_agent file section
    primary_prompt = runtime.calls[0]["user_prompt"]
    assert "Implementation files written by code_change_agent" in primary_prompt
    assert "app/service.py" in primary_prompt


# ---------------------------------------------------------------------------
# Gap #4: _build_last_command_failure_summary
# ---------------------------------------------------------------------------


def test_last_command_failure_summary_returns_failure_output(tmp_path: Path) -> None:
    request = _make_request(str(tmp_path))
    state = _make_state(request)
    state.evidence.add_command_execution(
        command="pytest tests/",
        producer="command_runner_agent",
        exit_code=1,
        stdout="FAILED tests/test_x.py::test_create - AssertionError: assert 404 == 201",
        stderr="",
    )

    summary = _build_last_command_failure_summary(state)

    assert "pytest tests/" in summary
    assert "exit_code: 1" in summary
    assert "AssertionError" in summary


def test_last_command_failure_summary_skips_successful_commands(tmp_path: Path) -> None:
    request = _make_request(str(tmp_path))
    state = _make_state(request)
    state.evidence.add_command_execution(
        command="pytest tests/",
        producer="command_runner_agent",
        exit_code=0,
        stdout="1 passed",
        stderr="",
    )

    summary = _build_last_command_failure_summary(state)

    assert "all commands succeeded" in summary.lower()


def test_last_command_failure_in_prompt_on_repair_pass(tmp_path: Path) -> None:
    """On repair pass (materialization_attempt_count > 0), failure output must reach LLM."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    request = _make_request(str(workspace))
    step = _make_step()
    state = _make_state(request)

    # Simulate a repair pass: the agent has already run once (count=1 pre-increment).
    # execute_step will increment it to 2, satisfying the > 1 condition.
    state.materialization_attempt_count = 1
    state.evidence.add_command_execution(
        command="pytest tests/",
        producer="command_runner_agent",
        exit_code=1,
        stdout="ModuleNotFoundError: No module named 'app'",
        stderr="",
    )

    good_payload = _materialisation_payload([("tests/conftest.py", "create", "import sys")])
    runtime = _CapturingRuntime([good_payload, _coverage_payload()])

    agent = TestBuilderAgent(runtime=runtime)
    agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    primary_prompt = runtime.calls[0]["user_prompt"]
    assert "Last command failure output" in primary_prompt
    assert "ModuleNotFoundError" in primary_prompt


def test_last_command_failure_not_applicable_on_first_attempt(tmp_path: Path) -> None:
    """On first attempt, the failure summary section says 'not applicable'."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    request = _make_request(str(workspace))
    step = _make_step()
    state = _make_state(request)
    # materialization_attempt_count starts at 0

    state.evidence.add_command_execution(
        command="pytest tests/",
        producer="command_runner_agent",
        exit_code=1,
        stdout="some failure",
        stderr="",
    )

    good_payload = _materialisation_payload([("tests/test_x.py", "create", "pass")])
    runtime = _CapturingRuntime([good_payload, _coverage_payload()])

    agent = TestBuilderAgent(runtime=runtime)
    agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    primary_prompt = runtime.calls[0]["user_prompt"]
    assert "not applicable" in primary_prompt.lower()


def test_execution_trace_written_for_needs_dependency(tmp_path: Path) -> None:
    from unittest.mock import patch

    workspace = str(tmp_path)
    request = _make_request(workspace)
    step = _make_step()
    state = _make_state(request)

    payload = _materialisation_payload([], needs_dependency="pytest-asyncio>=0.23")
    runtime = _CapturingRuntime([payload])

    captured: list[dict] = []

    def _fake_append(*, project_id, entry):
        captured.append(entry)

    with patch(
        "app.execution_engine.subagents.test_builder_agent.append_execution_trace",
        side_effect=_fake_append,
    ):
        agent = TestBuilderAgent(runtime=runtime)
        agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    assert len(captured) == 1
    entry = captured[0]
    assert entry["call_type"] == "needs_dependency"
    assert entry["needs_dependency"] == "pytest-asyncio>=0.23"
    assert entry["files_written"] == []
