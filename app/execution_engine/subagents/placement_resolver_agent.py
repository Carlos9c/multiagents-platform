from __future__ import annotations

from pydantic import ValidationError

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.capabilities import get_executor_capabilities
from app.execution_engine.contracts import ExecutionRequest
from app.execution_engine.execution_plan import (
    STEP_KIND_RESOLVE_FILE_OPERATIONS,
    ExecutionStep,
)
from app.execution_engine.file_operations import FileOperationPlan
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.services.llm.schema_utils import to_openai_strict_json_schema


PLACEMENT_RESOLVER_SYSTEM_PROMPT = """
You are a senior artifact placement and integration agent.

Your job is to decide which repository-relative artifacts should be created or modified
to execute ONE already-atomic task safely.

Return ONLY JSON matching the provided schema.

Hard rules:
- Do not change the task.
- Do not expand scope beyond the atomic task.
- Prefer the smallest coherent artifact surface.
- Use repository-relative paths only.
- If an existing artifact is the natural integration point, prefer modify.
- Create new artifacts when necessary.
- Multi-artifact changes are allowed when required for correct integration.
- Use sequence to express a sensible application order.
- Use depends_on_paths when one operation logically depends on another.
- Include integration notes when wiring or placement constraints matter.
- Do not validate completion. Validation happens outside the execution engine.
- Do not invent broad refactors.
- Do not reject just because there is uncertainty.

Critical rule:
- If the workspace is effectively empty BUT the executor supports artifact creation
  and bootstrap from an empty workspace, you must still produce a minimal valid
  artifact plan inferred from the task intent and executor capabilities.
- The absence of repository conventions is not, by itself, a valid reason to stop.
""".strip()


def _build_user_prompt(request: ExecutionRequest, state: ResolutionState) -> str:
    capabilities = get_executor_capabilities(request.executor_type)

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
- executor_type: {request.executor_type}

Executor capabilities:
- supports_artifact_creation: {capabilities.supports_artifact_creation}
- supports_artifact_modification: {capabilities.supports_artifact_modification}
- supports_bootstrap_from_empty_workspace: {capabilities.supports_bootstrap_from_empty_workspace}

Execution context:
- workspace_path: {request.context.workspace_path}
- source_path: {request.context.source_path}
- related_tasks: {[item.model_dump() for item in request.context.related_tasks]}
- relevant_files: {request.context.relevant_files}
- key_decisions: {request.context.key_decisions}
- allowed_paths: {request.allowed_paths}
- blocked_paths: {request.blocked_paths}

Observed repository summary:
{state.observed_repo_summary or "[no repository summary available]"}

Selected file context:
{state.selected_file_context or "[no selected file context available]"}

Important:
- Return a minimal but sufficient artifact plan.
- If the workspace is empty and bootstrap is supported, infer a sensible initial artifact set from the task objective.
- Do not stop just because no conventions are visible yet.
- If safe placement is genuinely impossible, explain why using rejection_reason.
""".strip()


class PlacementResolverAgent(BaseSubagent):
    name = "placement_resolver_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self.runtime = runtime

    def supports_step_kind(self, step_kind: str) -> bool:
        return step_kind == STEP_KIND_RESOLVE_FILE_OPERATIONS

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

        state.increment_file_planning_attempts()

        schema = to_openai_strict_json_schema(FileOperationPlan.model_json_schema())
        raw = self.runtime.generate_structured(
            system_prompt=PLACEMENT_RESOLVER_SYSTEM_PROMPT,
            user_prompt=_build_user_prompt(request, state),
            schema_name="execution_engine_file_operation_plan",
            json_schema=schema,
        )

        try:
            plan = FileOperationPlan.model_validate(raw)
        except ValidationError as exc:
            raise SubagentRejectedStepError(
                f"Invalid placement resolver output: {str(exc)}"
            ) from exc

        if plan.rejection_reason:
            raise SubagentRejectedStepError(
                f"Placement resolver rejected the task: {plan.rejection_reason}"
            )

        if not plan.operations:
            raise SubagentRejectedStepError(
                "Placement resolver returned no file operations."
            )

        state.set_planned_file_operations(plan)
        state.add_note("LLM-based file operation plan resolved.")
        state.add_risk_flags(plan.risks)
        state.evidence.notes.extend(plan.notes)

        selected_paths = [item.path for item in plan.sorted_operations()]
        state.add_selected_paths(selected_paths)

        return state