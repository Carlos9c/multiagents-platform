from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from app.execution_engine.contracts import (
    CHANGE_TYPE_MODIFIED,
    ChangedFile,
    ExecutionEvidence,
    ExecutionRequest,
    ProjectExecutionContext,
)
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import SubagentRejectedStepError
from app.execution_engine.subagents.command_runner_agent import CommandRunnerAgent
from app.execution_engine.tools.command_tool import CommandToolError, run_command


def test_run_command_executes_simple_process(tmp_path: Path):
    result = run_command(
        command=f'{sys.executable} -c "print(123)"',
        cwd=str(tmp_path),
    )

    assert result.exit_code == 0
    assert "123" in result.stdout
    assert result.stderr == ""


def test_run_command_rejects_shell_executable(tmp_path: Path):
    with pytest.raises(CommandToolError, match="Shell executables are not allowed"):
        run_command(
            command='powershell -Command "Write-Output 123"',
            cwd=str(tmp_path),
        )


def test_run_command_rejects_empty_command(tmp_path: Path):
    with pytest.raises(CommandToolError, match="cannot be empty"):
        run_command(
            command="   ",
            cwd=str(tmp_path),
        )


def test_run_command_rejects_non_positive_timeout(tmp_path: Path):
    with pytest.raises(CommandToolError, match="greater than zero"):
        run_command(
            command=f'{sys.executable} -c "print(1)"',
            cwd=str(tmp_path),
            timeout_seconds=0,
        )


def test_run_command_rejects_timeout_above_limit(tmp_path: Path):
    with pytest.raises(CommandToolError, match="cannot exceed"):
        run_command(
            command=f'{sys.executable} -c "print(1)"',
            cwd=str(tmp_path),
            timeout_seconds=901,
        )


def test_run_command_rejects_unknown_executable(tmp_path: Path):
    with pytest.raises(CommandToolError, match="not available on PATH"):
        run_command(
            command="definitely_not_a_real_executable_12345",
            cwd=str(tmp_path),
        )


def test_run_command_rejects_nonexistent_working_directory(tmp_path: Path):
    missing_dir = tmp_path / "missing-dir"

    with pytest.raises(FileNotFoundError, match="Working directory does not exist"):
        run_command(
            command=f'{sys.executable} -c "print(1)"',
            cwd=str(missing_dir),
        )


def test_run_command_rejects_path_argument_outside_execution_tree(tmp_path: Path):
    outside_file = tmp_path.parent / "outside.txt"
    outside_file.write_text("hello", encoding="utf-8")

    with pytest.raises(CommandToolError, match="outside the execution tree"):
        run_command(
            command=f'"{sys.executable}" "../outside.txt"',
            cwd=str(tmp_path),
        )


def test_run_command_returns_timeout_result_instead_of_raising(tmp_path: Path):
    result = run_command(
        command=f'{sys.executable} -c "import time; time.sleep(2)"',
        cwd=str(tmp_path),
        timeout_seconds=1,
    )

    assert result.exit_code == 124
    assert "timed out" in result.stderr.lower()


def test_run_command_truncates_large_stdout(tmp_path: Path):
    result = run_command(
        command=f"{sys.executable} -c \"print('x' * 40000)\"",
        cwd=str(tmp_path),
    )

    assert result.exit_code == 0
    assert "truncated" in result.stdout
    assert len(result.stdout) <= 32000


class FakeRuntime:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: dict,
    ) -> dict:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "schema_name": schema_name,
                "json_schema": json_schema,
            }
        )
        if not self._responses:
            raise AssertionError("FakeRuntime has no more structured responses.")
        return self._responses.pop(0)


def _build_request(*, task_id: int = 101, execution_run_id: int = 201) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=task_id,
        project_id=1,
        execution_run_id=execution_run_id,
        task_title="Definir especificación mínima de tareas",
        task_description=(
            "Crear una especificación funcional mínima en el repositorio para creación y listado "
            "de tareas con almacenamiento en memoria."
        ),
        task_summary="Especificación funcional mínima.",
        objective="Documentar el contrato funcional observable.",
        proposed_solution=None,
        implementation_notes=None,
        implementation_steps=None,
        acceptance_criteria=(
            "Existe un documento con modelo, creación, listado, validaciones y fuera de alcance."
        ),
        tests_required=None,
        technical_constraints="Sin dependencias externas.",
        out_of_scope="No autenticación, no persistencia externa.",
        executor_type="execution_engine",
        success_criteria=[],
        constraints=[],
        allowed_paths=["docs/especificacion-api-tareas.md"],
        blocked_paths=[],
        context=ProjectExecutionContext(
            project_id=1,
            workspace_path=".",
            source_path=".",
            relevant_files=["docs/especificacion-api-tareas.md"],
            key_decisions=["Mantener solución mínima."],
            related_tasks=[],
        ),
        historical_context=None,
    )


def _build_step() -> ExecutionStep:
    return ExecutionStep(
        id="dynamic_call_2_command_runner_agent",
        subagent_name="command_runner_agent",
        title="command_runner_agent",
        instructions="Verify whether a repository-local executable check would materially improve confidence.",
        target_paths=[],
        metadata={},
    )


def _build_state_with_changed_file(
    path: str = "docs/especificacion-api-tareas.md",
) -> ResolutionState:
    state = ResolutionState(
        execution_request=_build_request(),
    )
    state.phase = "execution"
    state.completed_steps = [
        "dynamic_call_0_context_selection_agent",
        "dynamic_call_1_code_change_agent",
    ]
    state.evidence = ExecutionEvidence(
        changed_files=[
            ChangedFile(
                path=path,
                change_type=CHANGE_TYPE_MODIFIED,
                producer="code_change_agent",
            )
        ],
        files_read=[],
        change_dependencies=[],
        commands=[],
        notes=[],
        artifacts_created=[],
    )
    return state


def _inspection_response(*selected_paths: str) -> dict:
    return {
        "selected_paths": list(selected_paths),
        "selection_rationale": (
            "Selected the repository files most relevant for planning repository-local verification."
        ),
        "verification_hypothesis": (
            "Reading these files should provide enough context to determine whether executable "
            "verification is applicable and, if so, which narrow command should be run."
        ),
    }


def test_command_runner_agent_records_not_applicable_without_executing_command(
    monkeypatch,
    tmp_path: Path,
):
    runtime = FakeRuntime(
        responses=[
            _inspection_response("docs/especificacion-api-tareas.md"),
            {
                "decision": "verification_not_applicable",
                "verification_goal": (
                    "Determine whether any repository-local executable verification is materially useful."
                ),
                "rationale": (
                    "This task produced a documentation/specification artifact only, and no meaningful "
                    "repository-local executable check would materially improve the evidence."
                ),
            },
        ]
    )
    agent = CommandRunnerAgent(runtime=runtime)
    request = _build_request()
    step = _build_step()
    state = _build_state_with_changed_file()

    run_tree = tmp_path / "run_tree"
    run_tree.mkdir(parents=True, exist_ok=True)
    docs_dir = run_tree / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "especificacion-api-tareas.md").write_text("# Spec\n", encoding="utf-8")

    called = {
        "materialize": 0,
        "cleanup": 0,
        "run_command": 0,
    }

    monkeypatch.setattr(
        agent.workspace_runtime,
        "materialize_run_tree",
        lambda **kwargs: called.__setitem__("materialize", called["materialize"] + 1) or run_tree,
    )
    monkeypatch.setattr(
        agent.workspace_runtime,
        "cleanup_run_tree",
        lambda **kwargs: called.__setitem__("cleanup", called["cleanup"] + 1),
    )

    def _boom_run_command(**kwargs):
        called["run_command"] += 1
        raise AssertionError("run_command should not be called when verification is not applicable")

    monkeypatch.setattr(
        "app.execution_engine.subagents.command_runner_agent.run_command",
        _boom_run_command,
    )

    updated_state = agent.execute_step(
        db=types.SimpleNamespace(),
        request=request,
        step=step,
        state=state,
    )

    assert updated_state is state
    assert called["materialize"] == 1
    assert called["cleanup"] == 1
    assert called["run_command"] == 0
    assert updated_state.evidence.commands == []

    note_messages = [note.message for note in updated_state.evidence.notes]
    assert any("not materially applicable" in message for message in note_messages)
    assert any("Verification goal assessment:" in message for message in note_messages)


def test_command_runner_agent_executes_command_when_verification_applies(
    monkeypatch,
    tmp_path: Path,
):
    runtime = FakeRuntime(
        responses=[
            _inspection_response("app/service.py"),
            {
                "decision": "run_command",
                "command": 'python -c "print(123)"',
                "cwd_relative_path": ".",
                "verification_goal": "Verify that the minimal executable check succeeds.",
                "rationale": "A narrow executable command materially improves confidence.",
                "validation_claims": ["smoke_check_passed"],
                "expected_exit_codes": [0],
            },
        ]
    )
    agent = CommandRunnerAgent(runtime=runtime)
    request = _build_request(task_id=102, execution_run_id=202)
    step = _build_step()
    state = _build_state_with_changed_file(path="app/service.py")

    run_tree = tmp_path / "run_tree"
    run_tree.mkdir(parents=True, exist_ok=True)
    (run_tree / "app").mkdir(parents=True, exist_ok=True)
    (run_tree / "app" / "service.py").write_text("print('ok')\n", encoding="utf-8")

    called = {
        "materialize": 0,
        "cleanup": 0,
        "run_command": 0,
    }

    monkeypatch.setattr(
        agent.workspace_runtime,
        "materialize_run_tree",
        lambda **kwargs: called.__setitem__("materialize", called["materialize"] + 1) or run_tree,
    )
    monkeypatch.setattr(
        agent.workspace_runtime,
        "cleanup_run_tree",
        lambda **kwargs: called.__setitem__("cleanup", called["cleanup"] + 1),
    )

    def _fake_run_command(*, command: str, cwd: str):
        called["run_command"] += 1
        assert command == 'python -c "print(123)"'
        assert Path(cwd).resolve() == run_tree.resolve()
        return types.SimpleNamespace(
            command=command,
            exit_code=0,
            stdout="123\n",
            stderr="",
        )

    monkeypatch.setattr(
        "app.execution_engine.subagents.command_runner_agent.run_command",
        _fake_run_command,
    )

    updated_state = agent.execute_step(
        db=types.SimpleNamespace(),
        request=request,
        step=step,
        state=state,
    )

    assert updated_state is state
    assert called["materialize"] == 1
    assert called["cleanup"] == 1
    assert called["run_command"] == 1
    assert len(updated_state.evidence.commands) == 1

    command_evidence = updated_state.evidence.commands[0]
    assert command_evidence.command == 'python -c "print(123)"'
    assert command_evidence.producer == "command_runner_agent"
    assert command_evidence.cwd == "."
    assert command_evidence.exit_code == 0
    assert (
        command_evidence.verification_goal == "Verify that the minimal executable check succeeds."
    )
    assert command_evidence.validation_claims == ["smoke_check_passed"]
    assert command_evidence.expected_exit_codes == [0]
    assert "matched expected_exit_codes=[0]" in command_evidence.observed_outcome_summary


def test_command_runner_agent_rejects_disallowed_shell_constructs_in_planned_command(
    monkeypatch,
    tmp_path: Path,
):
    # Both the initial plan AND the constraint retry return disallowed constructs →
    # the agent must ultimately raise after exhausting retries.
    runtime = FakeRuntime(
        responses=[
            _inspection_response("app/service.py"),
            {
                "decision": "run_command",
                "command": "pytest -q && python -m app",
                "cwd_relative_path": ".",
                "verification_goal": "Run verification.",
                "rationale": "Try two checks.",
                "validation_claims": ["verification_attempted"],
                "expected_exit_codes": [0],
            },
            # constraint retry also returns a bad command
            {
                "decision": "run_command",
                "command": "pytest -q | tee results.txt",
                "cwd_relative_path": ".",
                "verification_goal": "Run verification.",
                "rationale": "Still piping.",
                "validation_claims": ["verification_attempted"],
                "expected_exit_codes": [0],
            },
        ]
    )
    agent = CommandRunnerAgent(runtime=runtime)
    request = _build_request(task_id=103, execution_run_id=203)
    step = _build_step()
    state = _build_state_with_changed_file(path="app/service.py")

    run_tree = tmp_path / "run_tree"
    run_tree.mkdir(parents=True, exist_ok=True)
    (run_tree / "app").mkdir(parents=True, exist_ok=True)
    (run_tree / "app" / "service.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(
        agent.workspace_runtime,
        "materialize_run_tree",
        lambda **kwargs: run_tree,
    )
    monkeypatch.setattr(
        agent.workspace_runtime,
        "cleanup_run_tree",
        lambda **kwargs: None,
    )

    with pytest.raises(SubagentRejectedStepError, match="disallowed shell constructs"):
        agent.execute_step(
            db=types.SimpleNamespace(),
            request=request,
            step=step,
            state=state,
        )


def test_command_runner_agent_retries_on_disallowed_shell_constructs_and_succeeds(
    monkeypatch,
    tmp_path: Path,
):
    # Initial plan has disallowed constructs; constraint retry returns a valid single command.
    runtime = FakeRuntime(
        responses=[
            _inspection_response("app/service.py"),
            {
                "decision": "run_command",
                "command": "pytest -q && python -m app",
                "cwd_relative_path": ".",
                "verification_goal": "Run verification.",
                "rationale": "Try two checks.",
                "validation_claims": ["verification_attempted"],
                "expected_exit_codes": [0],
            },
            # constraint retry returns a single allowed command
            {
                "decision": "run_command",
                "command": 'python -c "print(123)"',
                "cwd_relative_path": ".",
                "verification_goal": "Verify the minimal executable check succeeds.",
                "rationale": "Single allowed command.",
                "validation_claims": ["smoke_check_passed"],
                "expected_exit_codes": [0],
            },
        ]
    )
    agent = CommandRunnerAgent(runtime=runtime)
    request = _build_request(task_id=110, execution_run_id=210)
    step = _build_step()
    state = _build_state_with_changed_file(path="app/service.py")

    run_tree = tmp_path / "run_tree"
    run_tree.mkdir(parents=True, exist_ok=True)
    (run_tree / "app").mkdir(parents=True, exist_ok=True)
    (run_tree / "app" / "service.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(
        agent.workspace_runtime,
        "materialize_run_tree",
        lambda **kwargs: run_tree,
    )
    monkeypatch.setattr(
        agent.workspace_runtime,
        "cleanup_run_tree",
        lambda **kwargs: None,
    )

    called = {"run_command": 0}

    def _fake_run_command(*, command: str, cwd: str):
        called["run_command"] += 1
        assert command == 'python -c "print(123)"'
        return types.SimpleNamespace(
            command=command,
            exit_code=0,
            stdout="123\n",
            stderr="",
        )

    monkeypatch.setattr(
        "app.execution_engine.subagents.command_runner_agent.run_command",
        _fake_run_command,
    )

    updated_state = agent.execute_step(
        db=types.SimpleNamespace(),
        request=request,
        step=step,
        state=state,
    )

    assert called["run_command"] == 1
    assert len(updated_state.evidence.commands) == 1
    assert updated_state.evidence.commands[0].exit_code == 0
    # Constraint retry prompt was generated (3 calls total: inspection, initial plan, retry)
    assert len(runtime.calls) == 3


def test_command_runner_agent_rejects_cwd_outside_run_tree(
    monkeypatch,
    tmp_path: Path,
):
    runtime = FakeRuntime(
        responses=[
            _inspection_response("app/service.py"),
            {
                "decision": "run_command",
                "command": 'python -c "print(123)"',
                "cwd_relative_path": "..",
                "verification_goal": "Run verification.",
                "rationale": "Attempt verification from parent directory.",
                "validation_claims": ["verification_attempted"],
                "expected_exit_codes": [0],
            },
        ]
    )
    agent = CommandRunnerAgent(runtime=runtime)
    request = _build_request(task_id=104, execution_run_id=204)
    step = _build_step()
    state = _build_state_with_changed_file(path="app/service.py")

    run_tree = tmp_path / "run_tree"
    run_tree.mkdir(parents=True, exist_ok=True)
    (run_tree / "app").mkdir(parents=True, exist_ok=True)
    (run_tree / "app" / "service.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(
        agent.workspace_runtime,
        "materialize_run_tree",
        lambda **kwargs: run_tree,
    )
    monkeypatch.setattr(
        agent.workspace_runtime,
        "cleanup_run_tree",
        lambda **kwargs: None,
    )

    with pytest.raises(SubagentRejectedStepError, match="escapes the candidate run tree"):
        agent.execute_step(
            db=types.SimpleNamespace(),
            request=request,
            step=step,
            state=state,
        )
