from __future__ import annotations

from pathlib import Path

import pytest
from app.models.task import EXECUTION_ENGINE
from app.execution_engine.agent_runtime.base import BaseAgentRuntime
from app.execution_engine.budget import LoopBudget
from app.execution_engine.contracts import (
    ChangedFile,
    ExecutionRequest,
    ProjectExecutionContext,
)
from app.execution_engine.file_operations import (
    FileMaterializationResult,
    FileOperation,
    FileOperationPlan,
    MaterializedFile,
)
from app.execution_engine.monitoring import OrchestratorTrace
from app.execution_engine.next_action import (
    ACTION_FINISH,
    ACTION_INSPECT_CONTEXT,
    NextActionDecision,
)
from app.execution_engine.orchestrator import ExecutionOrchestrator
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagent_registry import SubagentRegistry
from app.execution_engine.subagents.base import BaseSubagent
from app.execution_engine.subagents.code_change_agent import CodeChangeAgent


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

    def execute_step(self, *, request, step, state):
        state.selected_file_context = "selected context"
        state.mark_context_selected()
        state.add_note("stub context selection executed")
        return state


class StubPlacementResolverAgent(BaseSubagent):
    name = "placement_resolver_agent"

    def supports_step_kind(self, step_kind: str) -> bool:
        return step_kind == "resolve_file_operations"

    def execute_step(self, *, request, step, state):
        state.set_planned_file_operations(
            FileOperationPlan(
                summary="stub plan",
                operations=[
                    FileOperation(
                        operation="create",
                        path="docs/notes-api-contract.md",
                        reason="Create the required repository artifact.",
                        purpose="Contract artifact",
                        category="docs",
                        sequence=1,
                    )
                ],
            )
        )
        state.add_note("stub placement resolver executed")
        return state


class StubCodeChangeAgent(BaseSubagent):
    name = "code_change_agent"

    def supports_step_kind(self, step_kind: str) -> bool:
        return step_kind == "apply_file_operations"

    def execute_step(self, *, request, step, state):
        state.mark_operation_applied("docs/notes-api-contract.md")
        state.evidence.changed_files.append(
            ChangedFile(
                path="docs/notes-api-contract.md",
                change_type="created",
            )
        )
        state.evidence.notes.append("stub code change executed")
        return state


def _make_request(workspace_path: Path) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=1,
        project_id=1,
        execution_run_id=1,
        task_title="Implement notes API",
        task_description="Create API and related files.",
        task_summary="Implement notes API.",
        task_type="implementation",
        objective="Create a working notes API.",
        acceptance_criteria="The API exists and tests pass.",
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
    )


def test_file_operation_plan_sorted_operations_orders_by_sequence_category_and_path():
    plan = FileOperationPlan(
        summary="multi-file plan",
        operations=[
            FileOperation(
                operation="modify",
                path="tests/test_notes.py",
                reason="update tests",
                purpose="tests",
                category="test",
                sequence=3,
            ),
            FileOperation(
                operation="create",
                path="app/api/notes.py",
                reason="new endpoint module",
                purpose="source",
                category="source",
                sequence=1,
            ),
            FileOperation(
                operation="modify",
                path="app/main.py",
                reason="register router",
                purpose="integration",
                category="integration",
                sequence=2,
            ),
        ],
    )

    assert [item.path for item in plan.sorted_operations()] == [
        "app/api/notes.py",
        "app/main.py",
        "tests/test_notes.py",
    ]


def test_resolution_state_tracks_pending_operations(tmp_path):
    state = ResolutionState(
        orchestrator_trace=OrchestratorTrace(task_id=1),
    )

    plan = FileOperationPlan(
        summary="multi-file plan",
        operations=[
            FileOperation(
                operation="create",
                path="app/api/notes.py",
                reason="new endpoint module",
                purpose="source",
                category="source",
                sequence=1,
            ),
            FileOperation(
                operation="modify",
                path="app/main.py",
                reason="register router",
                purpose="integration",
                category="integration",
                sequence=2,
            ),
        ],
    )

    state.set_planned_file_operations(plan)

    assert state.phase == "materialization"
    assert state.pending_operation_paths == [
        "app/api/notes.py",
        "app/main.py",
    ]
    assert state.applied_operation_paths == []
    assert state.failed_operation_paths == []

    state.mark_operation_applied("app/api/notes.py")
    assert state.pending_operation_paths == ["app/main.py"]
    assert state.applied_operation_paths == ["app/api/notes.py"]
    assert state.phase == "materialization"

    state.mark_operation_applied("app/main.py")
    assert state.pending_operation_paths == []
    assert sorted(state.applied_operation_paths) == [
        "app/api/notes.py",
        "app/main.py",
    ]
    assert state.phase == "completion"
    assert state.has_pending_operations() is False


def test_code_change_agent_applies_pending_operations_and_marks_applied(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    existing_main = workspace / "app" / "main.py"
    existing_main.parent.mkdir(parents=True, exist_ok=True)
    existing_main.write_text("from fastapi import FastAPI\n\napp = FastAPI()\n", encoding="utf-8")

    request = _make_request(workspace)

    state = ResolutionState(
        orchestrator_trace=OrchestratorTrace(task_id=request.task_id),
        observed_repo_summary="repo summary",
        selected_file_context="selected file context",
        phase="materialization",
    )
    state.set_planned_file_operations(
        FileOperationPlan(
            summary="create endpoint and register router",
            operations=[
                FileOperation(
                    operation="create",
                    path="app/api/notes.py",
                    reason="new module",
                    purpose="notes endpoints",
                    category="source",
                    sequence=1,
                ),
                FileOperation(
                    operation="modify",
                    path="app/main.py",
                    reason="router wiring",
                    purpose="register router",
                    category="integration",
                    sequence=2,
                    depends_on_paths=["app/api/notes.py"],
                ),
            ],
        )
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
    assert "APIRouter" in (workspace / "app" / "api" / "notes.py").read_text(encoding="utf-8")
    assert "include_router" in (workspace / "app" / "main.py").read_text(encoding="utf-8")

    assert next_state.pending_operation_paths == []
    assert sorted(next_state.applied_operation_paths) == [
        "app/api/notes.py",
        "app/main.py",
    ]
    assert next_state.failed_operation_paths == []
    assert next_state.phase == "completion"
    assert sorted(item.path for item in next_state.evidence.changed_files) == [
        "app/api/notes.py",
        "app/main.py",
    ]


def test_code_change_agent_rolls_back_if_write_fails(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    main_file = workspace / "app" / "main.py"
    main_file.parent.mkdir(parents=True, exist_ok=True)
    original_main = "from fastapi import FastAPI\n\napp = FastAPI()\n"
    main_file.write_text(original_main, encoding="utf-8")

    request = _make_request(workspace)

    state = ResolutionState(
        orchestrator_trace=OrchestratorTrace(task_id=request.task_id),
        observed_repo_summary="repo summary",
        selected_file_context="selected file context",
        phase="materialization",
    )
    state.set_planned_file_operations(
        FileOperationPlan(
            summary="create endpoint and register router",
            operations=[
                FileOperation(
                    operation="create",
                    path="app/api/notes.py",
                    reason="new module",
                    purpose="notes endpoints",
                    category="source",
                    sequence=1,
                ),
                FileOperation(
                    operation="modify",
                    path="app/main.py",
                    reason="router wiring",
                    purpose="register router",
                    category="integration",
                    sequence=2,
                ),
            ],
        )
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
    assert "app/api/notes.py" in state.pending_operation_paths
    assert "app/main.py" in state.pending_operation_paths
    assert state.applied_operation_paths == []
    assert state.phase == "materialization"


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
                expected_outcome="Selected file context available.",
                risk_flags=[],
            ).model_dump(),
            NextActionDecision(
                action="resolve_file_operations",
                rationale="Need a file operation plan before finishing.",
                target_paths=[],
                command=None,
                expected_outcome="Artifact plan available.",
                risk_flags=[],
            ).model_dump(),
            NextActionDecision(
                action="apply_file_operations",
                rationale="Need to materialize the planned artifact.",
                target_paths=["docs/notes-api-contract.md"],
                command=None,
                expected_outcome="Artifact written to workspace.",
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
            StubPlacementResolverAgent(),
            StubCodeChangeAgent(),
        ]
    )

    orchestrator = ExecutionOrchestrator(
        runtime=runtime,
        registry=registry,
        budget=LoopBudget(max_steps=6),
    )

    result = orchestrator.run(request)

    assert result.decision == "partial"
    assert "Current operational pass is sufficient." in (result.details or "")
    assert result.evidence.changed_files
    joined_notes = "\n".join(result.evidence.notes)
    assert "orchestrator_started" in joined_notes
    assert "next_action_decided" in joined_notes
    assert "subagent_selected" in joined_notes
    assert "subagent_completed" in joined_notes
    assert "orchestrator_finished" in joined_notes


def test_orchestrator_phase_policy_prevents_return_to_context_after_planning(tmp_path):
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
                expected_outcome="Selected file context available.",
                risk_flags=[],
            ).model_dump(),
            NextActionDecision(
                action="resolve_file_operations",
                rationale="Need planning next.",
                target_paths=[],
                command=None,
                expected_outcome="Plan available.",
                risk_flags=[],
            ).model_dump(),
            NextActionDecision(
                action=ACTION_INSPECT_CONTEXT,
                rationale="Let's inspect again even though a plan exists.",
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
            StubPlacementResolverAgent(),
            StubCodeChangeAgent(),
        ]
    )

    orchestrator = ExecutionOrchestrator(
        runtime=runtime,
        registry=registry,
        budget=LoopBudget(max_steps=6),
    )

    result = orchestrator.run(request)

    assert result.decision == "partial"
    assert any(item.path == "docs/notes-api-contract.md" for item in result.evidence.changed_files)
    joined_notes = "\n".join(result.evidence.notes)
    assert "action_overridden_by_phase_policy" in joined_notes


def test_orchestrator_finish_with_pending_operations_reports_blocker(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    request = _make_request(workspace)

    class PendingFirstOrchestrator(ExecutionOrchestrator):
        def run(self, request):
            from app.execution_engine.monitoring import OrchestratorTrace
            from app.execution_engine.resolution_state import ResolutionState
            from app.execution_engine.state import ExecutionState

            runtime_state = ExecutionState()
            resolution_state = ResolutionState(
                orchestrator_trace=OrchestratorTrace(task_id=request.task_id),
                phase="completion",
            )
            resolution_state.pending_operation_paths = [
                "app/main.py",
                "tests/test_notes.py",
            ]
            resolution_state.applied_operation_paths = ["docs/bootstrap.md"]
            resolution_state.evidence.changed_files.append(
                ChangedFile(
                    path="docs/bootstrap.md",
                    change_type="created",
                )
            )

            self._decide_next_action(
                request=request,
                runtime_state=runtime_state,
                resolution_state=resolution_state,
            )

            remaining_scope = (
                "Some planned file operations remain pending."
                if resolution_state.has_pending_operations()
                else "External task validation remains pending."
            )

            blockers_found = (
                [f"pending_operations={','.join(resolution_state.pending_operation_paths)}"]
                if resolution_state.has_pending_operations()
                else []
            )

            return type(
                "Result",
                (),
                {
                    "decision": "partial",
                    "remaining_scope": remaining_scope,
                    "blockers_found": blockers_found,
                },
            )()

    runtime = FakeRuntime(
        responses=[
            NextActionDecision(
                action=ACTION_FINISH,
                rationale="Stop here and hand off.",
                target_paths=[],
                command=None,
                expected_outcome="External validation handles the rest.",
                risk_flags=[],
            ).model_dump(),
        ]
    )

    registry = SubagentRegistry(subagents=[StubContextSelectionAgent()])

    orchestrator = PendingFirstOrchestrator(
        runtime=runtime,
        registry=registry,
        budget=LoopBudget(max_steps=3),
    )

    result = orchestrator.run(request)

    assert result.decision == "partial"
    assert result.remaining_scope == "Some planned file operations remain pending."
    assert result.blockers_found == ["pending_operations=app/main.py,tests/test_notes.py"]
