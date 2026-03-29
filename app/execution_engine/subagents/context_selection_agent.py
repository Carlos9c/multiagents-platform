from __future__ import annotations

from pydantic import ValidationError

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.context_selection import ContextSelectionResult
from app.execution_engine.contracts import ExecutionRequest
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.execution_engine.tools.context_builder_tool import build_selected_file_context
from app.execution_engine.tools.workspace_scan_tool import list_workspace_files
from app.services.llm.schema_utils import to_openai_strict_json_schema

CONTEXT_SELECTION_SYSTEM_PROMPT = """
You are a senior code context selection agent.

Your job is to select the smallest high-value repository context needed to execute ONE already-atomic code task.

Return ONLY JSON matching the provided schema.

Hard rules:
- Do not change the task.
- Prefer a small but sufficient context set.
- Select project-relative file paths only.
- Prefer existing files that are likely integration points.
- Include full file content only when necessary.
- Use architectural notes and risks when helpful.
- Do not reject just because there are multiple plausible files; prefer the best candidates.
- Validation is outside the execution engine.
""".strip()


def _build_user_prompt(request: ExecutionRequest, state: ResolutionState) -> str:
    workspace_files = list_workspace_files(request.context.workspace_path)

    return f"""
Task:
- task_id: {request.task_id}
- title: {request.task_title}
- description: {request.task_description}
- summary: {request.task_summary}
- objective: {request.objective}
- acceptance_criteria: {request.acceptance_criteria}
- technical_constraints: {request.technical_constraints}
- out_of_scope: {request.out_of_scope}

Repository summary:
{state.observed_repo_summary or "[no repository summary available]"}

Workspace files:
{workspace_files}

Previously inferred candidate paths:
{state.candidate_paths}

Related tasks:
{[item.model_dump() for item in request.context.related_tasks]}

Important:
- Select the minimum useful set of files.
- Include files that are likely integration points.
- Include tests when materially relevant.
- Do not select huge amounts of context unnecessarily.
""".strip()


class ContextSelectionAgent(BaseSubagent):
    name = "context_selection_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self.runtime = runtime

    def supports_step_kind(self, step_kind: str) -> bool:
        return step_kind == "inspect_context"

    def execute_step(
        self,
        *,
        request: ExecutionRequest,
        step,
        state: ResolutionState,
    ) -> ResolutionState:
        schema = to_openai_strict_json_schema(ContextSelectionResult.model_json_schema())
        raw = self.runtime.generate_structured(
            system_prompt=CONTEXT_SELECTION_SYSTEM_PROMPT,
            user_prompt=_build_user_prompt(request, state),
            schema_name="execution_engine_context_selection",
            json_schema=schema,
        )

        try:
            result = ContextSelectionResult.model_validate(raw)
        except ValidationError as exc:
            raise SubagentRejectedStepError(
                f"Invalid context selection output: {str(exc)}"
            ) from exc

        state.context_selection = result
        state.add_risk_flags(result.risks)
        state.evidence.notes.extend(result.notes)

        selected_paths = [item.path for item in result.files]
        state.add_selected_paths(selected_paths)

        state.selected_file_context = build_selected_file_context(
            workspace_root=request.context.workspace_path,
            selection=result,
        )

        state.add_note("LLM-based context selection completed.")
        state.mark_context_selected()
        return state
