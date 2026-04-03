from __future__ import annotations

from pathlib import Path

import pytest

from app.execution_engine.agent_runtime.base import BaseAgentRuntime
from app.execution_engine.budget import LoopBudget
from app.execution_engine.context_selection import HistoricalTaskSelectionResult
from app.execution_engine.contracts import (
    ChangedFile,
    ExecutionRequest,
    ProjectExecutionContext,
)
from app.execution_engine.file_operations import (
    FileMaterializationResult,
    MaterializedFile,
)
from app.execution_engine.monitoring import OrchestratorTrace
from app.execution_engine.next_action import (
    ACTION_APPLY_FILE_OPERATIONS,
    ACTION_FINISH,
    ACTION_INSPECT_CONTEXT,
    NextActionDecision,
)
from app.execution_engine.orchestrator import ExecutionOrchestrator
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagent_registry import SubagentRegistry
from app.execution_engine.subagents.base import BaseSubagent
from app.execution_engine.subagents.code_change_agent import CodeChangeAgent
from app.models.task import EXECUTION_ENGINE


class FakeRuntime(BaseAgentRuntime):
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
            }
        )
        if not self._responses:
            raise RuntimeError("FakeRuntime has no more responses configured")
        return self._responses.pop(0)


class StubContextSelectionAgent(BaseSubagent):
    name = "context_selection_agent"

    def supports_step_kind(self, step_kind: str) -> bool:
        return step_kind == "inspect_context"

    def execute_step(self, *, db, request, step, state):
        state.set_historical_task_selection(
            HistoricalTaskSelectionResult(selected_task_runs=[])
        )
        state.add_note("stub context selection executed")
        state.mark_context_selected()
        return state


class StubCodeChangeAgent(BaseSubagent):
    name = "code_change_agent"

    def supports_step_kind(self, step_kind: str) -> bool:
        return step_kind == "apply_file_operations"

    def execute_step(self, *, db, request, step, state):
        state.evidence.changed_files.append(
            ChangedFile(
                path="docs/notes-api-contract.md",
                change_type="created",
            )
        )
        state.evidence.notes.append("stub code change executed")
        state.phase = "completion"
        return state


def _make_request(workspace_path: Path) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=1,
        project_id=1,
        execution_run_id=1,
        task_title="Implement notes API",
        task_description="Create API and related files.",
        task_summary="Implement notes API.",
        objective="Create a working notes API.",
        proposed_solution="Create a simple notes API surface.",
        implementation_notes="Prefer minimal coherent structure.",
        implementation_steps="Create files and wire them coherently.",
        acceptance_criteria="The API exists and tests pass.",
        tests_required="Add or update relevant tests if necessary.",
        technical_constraints="Use FastAPI.",
        out_of_scope="Persistence layer.",
        executor_type=EXECUTION_ENGINE,
        success_criteria=[],
        constraints=[],
        allowed_paths=[],
        blocked_paths=[],
        context=ProjectExecutionContext(
            project_id=1,
            source_path=str(workspace_path),
            workspace_path=str(workspace_path),
            relevant_files=[],
            key_decisions=[],
            related_tasks=[],
        ),
        historical_context=None,
    )


def test_code_change_agent_creates_and_modifies_files_without_prior_plan(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    existing_main = workspace / "app" / "main.py"
    existing_main.parent.mkdir(parents=True, exist_ok=True)
    existing_main.write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n",
        encoding="utf-8",
    )

    request = _make_request(workspace)

    state = ResolutionState(
        execution_request=request,
        orchestrator_trace=OrchestratorTrace(task_id=request.task_id),
        phase="execution",
    )

    runtime = FakeRuntime(
        responses=[
            FileMaterializationResult(
                summary="materialized",
                files=[
                    MaterializedFile(
                        path="app/api/notes.py",
                        operation="create",
                        content="from fastapi import APIRouter\n\nrouter = APIRouter()\n",
                        rationale="create endpoint module",
                    ),
                    MaterializedFile(
                        path="app/main.py",
                        operation="modify",
                        content=(
                            "from fastapi import FastAPI\n"
                            "from app.api.notes import router as notes_router\n\n"
                            "app = FastAPI()\n"
                            "app.include_router(notes_router)\n"
                        ),
                        rationale="wire router",
                    ),
                ],
                warnings=[],
                notes=["materialization completed"],
            ).model_dump()
        ]
    )

    agent = CodeChangeAgent(runtime=runtime)

    next_state = agent.execute_step(
        db=None,
        request=request,
        step=type(
            "Step",
            (),
            {
                "kind": "apply_file_operations",
            },
        )(),
        state=state,
    )

    assert (workspace / "app" / "api" / "notes.py").exists()
    assert "APIRouter" in (workspace / "app" / "api" / "notes.py").read_text(
        encoding="utf-8"
    )
    assert "include_router" in (workspace / "app" / "main.py").read_text(
        encoding="utf-8"
    )

    assert next_state.phase == "completion"
    assert sorted(item.path for item in next_state.evidence.changed_files) == [
        "app/api/notes.py",
        "app/main.py",
    ]
    assert "app/main.py" in next_state.evidence.files_read
    assert "materialization completed" in next_state.evidence.notes


def test_code_change_agent_rejects_modify_for_missing_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    request = _make_request(workspace)

    state = ResolutionState(
        execution_request=request,
        orchestrator_trace=OrchestratorTrace(task_id=request.task_id),
        phase="execution",
    )

    runtime = FakeRuntime(
        responses=[
            FileMaterializationResult(
                summary="invalid materialization",
                files=[
                    MaterializedFile(
                        path="app/api/notes.py",
                        operation="modify",
                        content="from fastapi import APIRouter\n",
                        rationale="should fail because file does not exist",
                    ),
                ],
                warnings=[],
                notes=[],
            ).model_dump()
        ]
    )

    agent = CodeChangeAgent(runtime=runtime)

    with pytest.raises(
        Exception,
        match="must be 'create' instead of 'modify'",
    ):
        agent.execute_step(
            db=None,
            request=request,
            step=type(
                "Step",
                (),
                {
                    "kind": "apply_file_operations",
                },
            )(),
            state=state,
        )


def test_code_change_agent_rolls_back_if_write_fails(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    main_file = workspace / "app" / "main.py"
    main_file.parent.mkdir(parents=True, exist_ok=True)
    original_main = "from fastapi import FastAPI\n\napp = FastAPI()\n"
    main_file.write_text(original_main, encoding="utf-8")

    request = _make_request(workspace)

    state = ResolutionState(
        execution_request=request,
        orchestrator_trace=OrchestratorTrace(task_id=request.task_id),
        phase="execution",
    )

    runtime = FakeRuntime(
        responses=[
            FileMaterializationResult(
                summary="materialized",
                files=[
                    MaterializedFile(
                        path="app/api/notes.py",
                        operation="create",
                        content="from fastapi import APIRouter\n\nrouter = APIRouter()\n",
                        rationale="create endpoint module",
                    ),
                    MaterializedFile(
                        path="app/main.py",
                        operation="modify",
                        content="BROKEN CONTENT",
                        rationale="wire router",
                    ),
                ],
                warnings=[],
                notes=[],
            ).model_dump()
        ]
    )

    from app.execution_engine.subagents import (
        code_change_agent as code_change_agent_module,
    )

    real_write = code_change_agent_module.write_text_file
    calls = {"count": 0}

    def failing_write(
        *, root_dir: str, relative_path: str, content: str, encoding: str = "utf-8"
    ) -> str:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated write failure")
        return real_write(
            root_dir=root_dir,
            relative_path=relative_path,
            content=content,
            encoding=encoding,
        )

    monkeypatch.setattr(code_change_agent_module, "write_text_file", failing_write)

    agent = CodeChangeAgent(runtime=runtime)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        agent.execute_step(
            db=None,
            request=request,
            step=type(
                "Step",
                (),
                {
                    "kind": "apply_file_operations",
                },
            )(),
            state=state,
        )

    assert not (workspace / "app" / "api" / "notes.py").exists()
    assert main_file.read_text(encoding="utf-8") == original_main


def test_orchestrator_records_trace_and_finishes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    request = _make_request(workspace)

    runtime = FakeRuntime(
        responses=[
            NextActionDecision(
                action=ACTION_INSPECT_CONTEXT,
                rationale="Need context first.",
                target_paths=[],
                command=None,
                expected_outcome="Execution request enriched.",
                risk_flags=[],
            ).model_dump(),
            NextActionDecision(
                action=ACTION_APPLY_FILE_OPERATIONS,
                rationale="Implementation can begin now.",
                target_paths=[],
                command=None,
                expected_outcome="Artifacts materialized in workspace.",
                risk_flags=[],
            ).model_dump(),
            NextActionDecision(
                action=ACTION_FINISH,
                rationale="Current operational pass is sufficient.",
                target_paths=[],
                command=None,
                expected_outcome="Return for external validation.",
                risk_flags=["low_confidence_on_context_coverage"],
            ).model_dump(),
        ]
    )

    registry = SubagentRegistry(
        subagents=[
            StubContextSelectionAgent(),
            StubCodeChangeAgent(),
        ]
    )

    orchestrator = ExecutionOrchestrator(
        runtime=runtime,
        registry=registry,
        budget=LoopBudget(max_steps=6),
    )

    result = orchestrator.run(db=None, request=request)

    assert result.decision == "partial"
    assert "Current operational pass is sufficient." in (result.details or "")
    assert result.evidence.changed_files
    joined_notes = "\n".join(result.evidence.notes)
    assert "orchestrator_started" in joined_notes
    assert "next_action_decided" in joined_notes
    assert "subagent_selected" in joined_notes
    assert "subagent_completed" in joined_notes
    assert "orchestrator_finished" in joined_notes


def test_orchestrator_phase_policy_prevents_return_to_context_after_execution_started(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    request = _make_request(workspace)

    runtime = FakeRuntime(
        responses=[
            NextActionDecision(
                action=ACTION_INSPECT_CONTEXT,
                rationale="Need context first.",
                target_paths=[],
                command=None,
                expected_outcome="Context ready.",
                risk_flags=[],
            ).model_dump(),
            NextActionDecision(
                action=ACTION_INSPECT_CONTEXT,
                rationale="Let's inspect again even though execution should begin.",
                target_paths=[],
                command=None,
                expected_outcome="More context.",
                risk_flags=[],
            ).model_dump(),
            NextActionDecision(
                action=ACTION_FINISH,
                rationale="Now finish.",
                target_paths=[],
                command=None,
                expected_outcome="Done.",
                risk_flags=[],
            ).model_dump(),
        ]
    )

    registry = SubagentRegistry(
        subagents=[
            StubContextSelectionAgent(),
            StubCodeChangeAgent(),
        ]
    )

    orchestrator = ExecutionOrchestrator(
        runtime=runtime,
        registry=registry,
        budget=LoopBudget(max_steps=6),
    )

    result = orchestrator.run(db=None, request=request)

    assert result.decision == "partial"
    joined_notes = "\n".join(result.evidence.notes)
    assert "action_overridden_by_phase_policy" in joined_notes