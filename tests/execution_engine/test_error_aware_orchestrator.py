"""
Unit tests for Phase 3: error-aware orchestrator + dynamic context expansion.

Covers:
- _latest_error_diagnosis_observation: returns most recent error_diagnosis payload
- _expand_context_from_error_diagnosis: loads newly_discovered_files into preloaded_dependency_files
- _expand_context_from_error_diagnosis: idempotent (already-loaded files not re-read)
- _expand_context_from_error_diagnosis: graceful when files can't be found
- _expand_context_from_error_diagnosis: returns False when no diagnosis present
- _render_error_diagnosis_for_prompt: returns placeholder when no diagnosis
- _failed_verification_is_repairable_by_repo_changes: structured diagnosis priority over string-matching
- _render_error_diagnosis_for_prompt: includes key fields when diagnosis present
- Orchestrator loop calls _expand_context_from_error_diagnosis before deciding
- _build_orchestrator_prompt includes latest_error_diagnosis section
"""

from __future__ import annotations

from pathlib import Path

from app.execution_engine.contracts import (
    OBSERVATION_TYPE_ERROR_DIAGNOSIS,
    ExecutionEvidence,
    ExecutionRequest,
    HistoricalExecutionContext,
    ProjectExecutionContext,
)
from app.execution_engine.orchestrator import (
    _expand_context_from_error_diagnosis,
    _failed_verification_is_repairable_by_repo_changes,
    _latest_error_diagnosis_observation,
    _render_error_diagnosis_for_prompt,
)
from app.execution_engine.resolution_state import ResolutionState
from app.models.task import EXECUTION_ENGINE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    *,
    workspace_path: str,
    source_path: str | None = None,
    preloaded: dict[str, str] | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=1,
        project_id=1,
        execution_run_id=1,
        task_title="Fix compilation failure",
        task_description="Fix the import error in app/service.py.",
        task_summary="Bug fix.",
        objective="Make the code compile.",
        acceptance_criteria="No import errors.",
        technical_constraints="Python 3.12.",
        executor_type=EXECUTION_ENGINE,
        context=ProjectExecutionContext(
            project_id=1,
            workspace_path=workspace_path,
            source_path=source_path or workspace_path,
            relevant_files=[],
            key_decisions=[],
            related_tasks=[],
            preloaded_dependency_files=preloaded or {},
        ),
        historical_context=HistoricalExecutionContext(selected_task_runs=[]),
    )


def _make_state(request: ExecutionRequest) -> ResolutionState:
    state = ResolutionState(execution_request=request)
    state.phase = "execution"
    return state


def _add_error_diagnosis_observation(
    evidence: ExecutionEvidence,
    *,
    error_class: str = "compilation_failure",
    repair_directive: str = "Fix the import in app/service.py",
    newly_discovered_files: list[str] | None = None,
    is_recoverable: bool = True,
) -> None:
    payload = {
        "overall_error_class": error_class,
        "is_recoverable": is_recoverable,
        "repair_directive": repair_directive,
        "cross_file_root_cause": None,
        "newly_discovered_files": newly_discovered_files or [],
        "root_cause_errors": [
            {
                "error_class": error_class,
                "affected_file": "app/service.py",
                "line_number": 5,
                "symbol": "UserService",
                "error_message": "cannot import name 'UserService'",
                "is_cascade": False,
                "cascade_source_file": None,
            }
        ],
        "cascade_errors": [],
    }
    evidence.add_observation(
        evidence_type=OBSERVATION_TYPE_ERROR_DIAGNOSIS,
        producer="command_runner_agent",
        summary=repair_directive,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# _latest_error_diagnosis_observation
# ---------------------------------------------------------------------------


def test_latest_error_diagnosis_returns_none_when_empty():
    evidence = ExecutionEvidence()
    assert _latest_error_diagnosis_observation(evidence) is None


def test_latest_error_diagnosis_returns_payload():
    evidence = ExecutionEvidence()
    _add_error_diagnosis_observation(evidence, error_class="compilation_failure")
    result = _latest_error_diagnosis_observation(evidence)
    assert result is not None
    assert result["overall_error_class"] == "compilation_failure"


def test_latest_error_diagnosis_returns_most_recent():
    evidence = ExecutionEvidence()
    _add_error_diagnosis_observation(
        evidence,
        error_class="compilation_failure",
        repair_directive="first",
    )
    _add_error_diagnosis_observation(
        evidence,
        error_class="assertion_failure",
        repair_directive="second",
    )
    result = _latest_error_diagnosis_observation(evidence)
    assert result is not None
    assert result["overall_error_class"] == "assertion_failure"
    assert result["repair_directive"] == "second"


def test_latest_error_diagnosis_ignores_other_observation_types():
    evidence = ExecutionEvidence()
    evidence.add_observation(
        evidence_type="test_coverage_observation",
        producer="test_builder_agent",
        summary="coverage",
        payload={"covered_cases": []},
    )
    assert _latest_error_diagnosis_observation(evidence) is None


# ---------------------------------------------------------------------------
# _expand_context_from_error_diagnosis
# ---------------------------------------------------------------------------


def test_expand_context_loads_newly_discovered_file(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app").mkdir()
    service_file = workspace / "app" / "service.py"
    service_file.write_text("class UserService: pass")

    request = _make_request(workspace_path=str(workspace))
    state = _make_state(request)
    _add_error_diagnosis_observation(
        state.evidence,
        newly_discovered_files=["app/service.py"],
    )

    expanded = _expand_context_from_error_diagnosis(resolution_state=state)

    assert expanded is True
    loaded = state.execution_request.context.preloaded_dependency_files
    assert "app/service.py" in loaded
    assert "class UserService" in loaded["app/service.py"]


def test_expand_context_reads_from_source_when_not_in_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()
    (source / "app").mkdir()
    (source / "app" / "models.py").write_text("class User: pass")

    request = _make_request(workspace_path=str(workspace), source_path=str(source))
    state = _make_state(request)
    _add_error_diagnosis_observation(
        state.evidence,
        newly_discovered_files=["app/models.py"],
    )

    expanded = _expand_context_from_error_diagnosis(resolution_state=state)

    assert expanded is True
    loaded = state.execution_request.context.preloaded_dependency_files
    assert "app/models.py" in loaded
    assert "class User" in loaded["app/models.py"]


def test_expand_context_workspace_has_priority_over_source(tmp_path: Path):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()
    (workspace / "app").mkdir()
    (source / "app").mkdir()
    (workspace / "app" / "service.py").write_text("# workspace version")
    (source / "app" / "service.py").write_text("# source version")

    request = _make_request(workspace_path=str(workspace), source_path=str(source))
    state = _make_state(request)
    _add_error_diagnosis_observation(
        state.evidence,
        newly_discovered_files=["app/service.py"],
    )

    _expand_context_from_error_diagnosis(resolution_state=state)

    loaded = state.execution_request.context.preloaded_dependency_files
    assert loaded["app/service.py"] == "# workspace version"


def test_expand_context_is_idempotent(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app").mkdir()
    (workspace / "app" / "service.py").write_text("class UserService: pass")

    preloaded = {"app/service.py": "already loaded content"}
    request = _make_request(workspace_path=str(workspace), preloaded=preloaded)
    state = _make_state(request)
    _add_error_diagnosis_observation(
        state.evidence,
        newly_discovered_files=["app/service.py"],
    )

    # All files already loaded → returns False, does not overwrite
    expanded = _expand_context_from_error_diagnosis(resolution_state=state)

    assert expanded is False
    assert (
        state.execution_request.context.preloaded_dependency_files["app/service.py"]
        == "already loaded content"
    )


def test_expand_context_returns_false_when_no_diagnosis(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _make_state(request)

    expanded = _expand_context_from_error_diagnosis(resolution_state=state)
    assert expanded is False


def test_expand_context_returns_false_when_no_new_files(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _make_state(request)
    _add_error_diagnosis_observation(state.evidence, newly_discovered_files=[])

    expanded = _expand_context_from_error_diagnosis(resolution_state=state)
    assert expanded is False


def test_expand_context_graceful_when_file_missing(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _make_state(request)
    _add_error_diagnosis_observation(
        state.evidence,
        newly_discovered_files=["app/nonexistent.py"],
    )

    # Should not raise; returns False because no files could be read
    expanded = _expand_context_from_error_diagnosis(resolution_state=state)
    assert expanded is False


def test_expand_context_blocks_path_traversal(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _make_state(request)
    # A path that would escape the workspace
    _add_error_diagnosis_observation(
        state.evidence,
        newly_discovered_files=["../outside.py"],
    )

    # Should not raise; the path escapes the workspace → not loaded
    expanded = _expand_context_from_error_diagnosis(resolution_state=state)
    assert expanded is False


def test_expand_context_preserves_existing_preloaded_files(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app").mkdir()
    (workspace / "app" / "models.py").write_text("class Model: pass")

    preloaded = {"app/existing.py": "existing content"}
    request = _make_request(workspace_path=str(workspace), preloaded=preloaded)
    state = _make_state(request)
    _add_error_diagnosis_observation(
        state.evidence,
        newly_discovered_files=["app/models.py"],
    )

    _expand_context_from_error_diagnosis(resolution_state=state)

    loaded = state.execution_request.context.preloaded_dependency_files
    assert "app/existing.py" in loaded  # preserved
    assert "app/models.py" in loaded  # newly added


# ---------------------------------------------------------------------------
# _render_error_diagnosis_for_prompt
# ---------------------------------------------------------------------------


def test_render_error_diagnosis_placeholder_when_no_diagnosis():
    evidence = ExecutionEvidence()
    rendered = _render_error_diagnosis_for_prompt(evidence)
    assert "none" in rendered.lower()


def test_render_error_diagnosis_includes_error_class():
    evidence = ExecutionEvidence()
    _add_error_diagnosis_observation(
        evidence,
        error_class="compilation_failure",
        repair_directive="Fix the missing import in app/service.py",
    )
    rendered = _render_error_diagnosis_for_prompt(evidence)
    assert "compilation_failure" in rendered
    assert "Fix the missing import" in rendered


def test_render_error_diagnosis_includes_is_recoverable():
    evidence = ExecutionEvidence()
    _add_error_diagnosis_observation(
        evidence,
        error_class="network_error",
        is_recoverable=False,
    )
    rendered = _render_error_diagnosis_for_prompt(evidence)
    assert "False" in rendered or "false" in rendered.lower()


def test_render_error_diagnosis_includes_newly_discovered_files():
    evidence = ExecutionEvidence()
    _add_error_diagnosis_observation(
        evidence,
        newly_discovered_files=["app/service.py", "app/models.py"],
    )
    rendered = _render_error_diagnosis_for_prompt(evidence)
    assert "app/service.py" in rendered
    assert "app/models.py" in rendered


def test_render_error_diagnosis_includes_root_cause_errors():
    evidence = ExecutionEvidence()
    _add_error_diagnosis_observation(
        evidence,
        error_class="compilation_failure",
    )
    rendered = _render_error_diagnosis_for_prompt(evidence)
    assert "app/service.py" in rendered or "root_cause" in rendered.lower()


# ---------------------------------------------------------------------------
# Orchestrator prompt includes error diagnosis section
# ---------------------------------------------------------------------------


def test_orchestrator_prompt_includes_latest_error_diagnosis_section(tmp_path: Path):
    """
    _build_orchestrator_prompt should include the 'latest_error_diagnosis' section.
    We verify it by building the prompt and checking for the section marker.
    """
    from app.execution_engine.monitoring import OrchestratorTrace
    from app.execution_engine.orchestrator import _build_orchestrator_prompt
    from app.execution_engine.state import ExecutionState

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _make_state(request)
    state.orchestrator_trace = OrchestratorTrace(task_id=request.task_id)
    _add_error_diagnosis_observation(
        state.evidence,
        error_class="compilation_failure",
        repair_directive="Fix the import error",
    )
    runtime_state = ExecutionState()

    prompt = _build_orchestrator_prompt(request, runtime_state, state)

    assert "latest_error_diagnosis" in prompt
    assert "compilation_failure" in prompt
    assert "Fix the import error" in prompt


def test_orchestrator_prompt_shows_none_when_no_error_diagnosis(tmp_path: Path):
    from app.execution_engine.monitoring import OrchestratorTrace
    from app.execution_engine.orchestrator import _build_orchestrator_prompt
    from app.execution_engine.state import ExecutionState

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _make_state(request)
    state.orchestrator_trace = OrchestratorTrace(task_id=request.task_id)
    runtime_state = ExecutionState()

    prompt = _build_orchestrator_prompt(request, runtime_state, state)

    assert "latest_error_diagnosis" in prompt
    assert "none" in prompt.lower()


# ---------------------------------------------------------------------------
# _failed_verification_is_repairable_by_repo_changes — structured diagnosis
# ---------------------------------------------------------------------------


class _FakeCommand:
    """Minimal command object for testing."""

    def __init__(
        self,
        *,
        exit_code: int = 1,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        observed_outcome_summary: str = "",
        expected_exit_codes: list[int] | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.observed_outcome_summary = observed_outcome_summary
        self.expected_exit_codes = expected_exit_codes or []


def _state_with_changed_file(request: ExecutionRequest) -> ResolutionState:
    state = _make_state(request)
    state.evidence.add_changed_file(
        path="app/service.py",
        change_type="modified",
        producer="code_change_agent",
    )
    return state


def test_repairable_uses_diagnosis_is_recoverable_true(tmp_path):
    """When diagnosis says is_recoverable=True, the failure is repairable."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _state_with_changed_file(request)
    _add_error_diagnosis_observation(state.evidence, is_recoverable=True)

    cmd = _FakeCommand(stderr="completely unrecognised error text here")
    result = _failed_verification_is_repairable_by_repo_changes(
        request=request, resolution_state=state, command=cmd
    )
    assert result is True


def test_repairable_uses_diagnosis_is_recoverable_false(tmp_path):
    """When diagnosis says is_recoverable=False, the failure is NOT repairable
    even if the stdout/stderr contain repairable string markers."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _state_with_changed_file(request)
    _add_error_diagnosis_observation(state.evidence, is_recoverable=False)

    # Even with a marker that would pass the string-matching fallback:
    cmd = _FakeCommand(stderr="ModuleNotFoundError: No module named 'app'")
    result = _failed_verification_is_repairable_by_repo_changes(
        request=request, resolution_state=state, command=cmd
    )
    assert result is False


def test_repairable_diagnosis_requires_changed_files(tmp_path):
    """Even when diagnosis says is_recoverable=True, no changed files → not repairable."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    # State with NO changed files
    state = _make_state(request)
    _add_error_diagnosis_observation(state.evidence, is_recoverable=True)

    cmd = _FakeCommand(stderr="some error")
    result = _failed_verification_is_repairable_by_repo_changes(
        request=request, resolution_state=state, command=cmd
    )
    assert result is False


def test_repairable_fallback_to_string_matching_when_no_diagnosis(tmp_path):
    """When no diagnosis observation exists, string-matching still works."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _state_with_changed_file(request)
    # No error_diagnosis observation added

    cmd = _FakeCommand(stderr="ModuleNotFoundError: No module named 'app.service'")
    result = _failed_verification_is_repairable_by_repo_changes(
        request=request, resolution_state=state, command=cmd
    )
    assert result is True


def test_repairable_fallback_unrecognised_error_returns_false_without_diagnosis(tmp_path):
    """Without a diagnosis and without recognised markers, returns False."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _state_with_changed_file(request)

    cmd = _FakeCommand(stderr="SIGKILL: process killed")
    result = _failed_verification_is_repairable_by_repo_changes(
        request=request, resolution_state=state, command=cmd
    )
    assert result is False


def test_repairable_succeeded_command_always_false(tmp_path):
    """A succeeded command is never repairable (exit 0)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _state_with_changed_file(request)
    _add_error_diagnosis_observation(state.evidence, is_recoverable=True)

    cmd = _FakeCommand(exit_code=0, stderr="")
    result = _failed_verification_is_repairable_by_repo_changes(
        request=request, resolution_state=state, command=cmd
    )
    assert result is False


def test_repairable_none_command_always_false(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _make_request(workspace_path=str(workspace))
    state = _state_with_changed_file(request)
    _add_error_diagnosis_observation(state.evidence, is_recoverable=True)

    result = _failed_verification_is_repairable_by_repo_changes(
        request=request, resolution_state=state, command=None
    )
    assert result is False
