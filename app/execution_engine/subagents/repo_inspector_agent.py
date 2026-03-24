from __future__ import annotations

from pathlib import Path

from app.execution_engine.contracts import ExecutionRequest
from app.execution_engine.execution_plan import STEP_KIND_INSPECT_CONTEXT, ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.execution_engine.tools.repo_tree_tool import build_repo_tree_snapshot


class RepoInspectorAgent(BaseSubagent):
    name = "repo_inspector_agent"

    def supports_step_kind(self, step_kind: str) -> bool:
        return step_kind == STEP_KIND_INSPECT_CONTEXT

    def execute_step(
        self,
        *,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        if not self.supports_step_kind(step.kind):
            raise SubagentRejectedStepError(
                f"{self.name} does not support step kind '{step.kind}'"
            )

        workspace_path = request.context.workspace_path
        if not workspace_path:
            raise SubagentRejectedStepError(
                "Workspace path is missing in execution request context."
            )

        snapshot = build_repo_tree_snapshot(workspace_path)
        state.observed_repo_summary = snapshot
        state.add_note("Repository structure snapshot collected.")

        candidate_paths = self._infer_candidate_paths(workspace_path)
        state.add_candidate_paths(candidate_paths)

        state.evidence.notes.append("Repository inspection completed.")
        return state

    def _infer_candidate_paths(self, workspace_path: str) -> list[str]:
        root = Path(workspace_path)
        if not root.exists():
            return []

        candidates: list[str] = []

        preferred_dirs = [
            "app",
            "src",
            "tests",
            "backend",
            "api",
        ]

        for directory in preferred_dirs:
            candidate = root / directory
            if candidate.exists():
                candidates.append(directory)

        return candidates