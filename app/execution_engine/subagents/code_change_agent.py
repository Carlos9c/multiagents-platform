from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.contracts import (
    CHANGE_TYPE_CREATED,
    CHANGE_TYPE_MODIFIED,
    ChangedFile,
    ExecutionRequest,
)
from app.execution_engine.execution_plan import (
    STEP_KIND_APPLY_FILE_OPERATIONS,
    ExecutionStep,
)
from app.execution_engine.file_operations import (
    FileMaterializationResult,
    FileOperationPlan,
)
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.execution_engine.tools.file_reader_tool import read_text_file
from app.execution_engine.tools.file_snapshot_tool import (
    capture_file_snapshot,
    restore_file_snapshot,
)
from app.execution_engine.tools.file_writer_tool import write_text_file
from app.services.llm.schema_utils import to_openai_strict_json_schema

CODE_CHANGE_AGENT_SYSTEM_PROMPT = """
You are a senior artifact implementation agent.

Your job is to materialize the approved pending artifact operation plan for ONE already-atomic task.
Return ONLY JSON matching the provided schema.

Hard rules:
- Do not change the task.
- Generate exactly the artifacts in the provided pending operation plan.
- For operation=create, return the full final artifact content.
- For operation=modify, return the full updated artifact content.
- Do not invent extra artifacts.
- Respect sequence, dependencies, repository structure, and integration notes.
- Keep the implementation as small and coherent as possible.
- Do not validate final completion. Validation happens outside the execution engine.
""".strip()


def _build_existing_files_context(
    *,
    workspace_root: str,
    plan: FileOperationPlan,
) -> str:
    parts: list[str] = []

    for item in plan.sorted_operations():
        abs_path = Path(workspace_root) / item.path
        parts.append(f"- path: {item.path}")
        parts.append(f"  operation: {item.operation}")
        parts.append(f"  category: {item.category}")
        parts.append(f"  sequence: {item.sequence}")
        parts.append(f"  purpose: {item.purpose}")
        parts.append(f"  reason: {item.reason}")
        parts.append(f"  depends_on_paths: {item.depends_on_paths}")
        parts.append(f"  edit_mode: {item.edit_mode}")
        parts.append(f"  symbols_expected: {item.symbols_expected}")

        if item.integration_notes:
            parts.append("  integration_notes:")
            parts.extend([f"    - {note}" for note in item.integration_notes])

        if abs_path.exists() and abs_path.is_file():
            try:
                content = read_text_file(str(abs_path))
                parts.append("  existing_content:")
                parts.append(content)
            except Exception as exc:
                parts.append(f"  existing_content_error: {str(exc)}")
        else:
            parts.append("  existing_content: [missing file]")

    return "\n".join(parts)


def _build_user_prompt(
    request: ExecutionRequest,
    state: ResolutionState,
    pending_plan: FileOperationPlan,
) -> str:
    files_context = _build_existing_files_context(
        workspace_root=request.context.workspace_path,
        plan=pending_plan,
    )

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

Selected file context:
{state.selected_file_context or "[no selected file context available]"}

Approved pending artifact operation plan:
{pending_plan.model_dump_json(indent=2)}

Current operation tracking:
- pending_operation_paths: {state.pending_operation_paths}
- applied_operation_paths: {state.applied_operation_paths}
- failed_operation_paths: {state.failed_operation_paths}

Existing file context:
{files_context}

Important:
- Generate exactly the artifacts in the pending plan.
- Return full final content for each artifact.
- Respect operation order and dependencies.
- Keep the implementation conservative and scoped.
""".strip()


class CodeChangeAgent(BaseSubagent):
    name = "code_change_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self.runtime = runtime

    def supports_step_kind(self, step_kind: str) -> bool:
        return step_kind == STEP_KIND_APPLY_FILE_OPERATIONS

    def execute_step(
        self,
        *,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        if not self.supports_step_kind(step.kind):
            raise SubagentRejectedStepError(f"{self.name} does not support step kind '{step.kind}'")

        if state.planned_file_operations is None:
            raise SubagentRejectedStepError(
                "No planned file operations are available to materialize."
            )

        pending_plan = state.get_pending_plan_subset()
        if pending_plan is None or not pending_plan.operations:
            raise SubagentRejectedStepError("There are no pending file operations to materialize.")

        state.increment_materialization_attempts()

        schema = to_openai_strict_json_schema(FileMaterializationResult.model_json_schema())
        raw = self.runtime.generate_structured(
            system_prompt=CODE_CHANGE_AGENT_SYSTEM_PROMPT,
            user_prompt=_build_user_prompt(request, state, pending_plan),
            schema_name="execution_engine_file_materialization",
            json_schema=schema,
        )

        try:
            materialization = FileMaterializationResult.model_validate(raw)
        except ValidationError as exc:
            raise SubagentRejectedStepError(f"Invalid code change output: {str(exc)}") from exc

        expected_paths = {item.path for item in pending_plan.sorted_operations()}
        returned_paths = {item.path for item in materialization.files}

        if expected_paths != returned_paths:
            raise SubagentRejectedStepError(
                "Generated files do not match the pending file operation plan."
            )

        planned_by_path = {item.path: item for item in pending_plan.sorted_operations()}

        ordered_generated_files = sorted(
            materialization.files,
            key=lambda item: planned_by_path[item.path].sequence,
        )

        snapshot_paths = [item.path for item in ordered_generated_files]
        snapshots = capture_file_snapshot(
            root_dir=request.context.workspace_path,
            relative_paths=snapshot_paths,
        )

        successfully_written_paths: list[str] = []

        try:
            for generated in ordered_generated_files:
                planned = planned_by_path[generated.path]

                if generated.operation != planned.operation:
                    state.mark_operation_failed(generated.path)
                    raise SubagentRejectedStepError(
                        f"Generated operation mismatch for '{generated.path}'. "
                        f"expected={planned.operation} actual={generated.operation}"
                    )

                absolute_path = write_text_file(
                    root_dir=request.context.workspace_path,
                    relative_path=generated.path,
                    content=generated.content,
                )

                successfully_written_paths.append(generated.path)
                state.mark_operation_applied(generated.path)

                change_type = (
                    CHANGE_TYPE_CREATED if generated.operation == "create" else CHANGE_TYPE_MODIFIED
                )

                state.evidence.changed_files.append(
                    ChangedFile(path=generated.path, change_type=change_type)
                )
                state.evidence.notes.append(f"Wrote file {generated.path} at {absolute_path}")

        except Exception:
            restore_file_snapshot(
                root_dir=request.context.workspace_path,
                snapshots=snapshots,
            )

            for path in successfully_written_paths:
                if path in state.applied_operation_paths:
                    state.applied_operation_paths.remove(path)
                if path not in state.pending_operation_paths:
                    state.pending_operation_paths.append(path)

            raise

        state.evidence.notes.extend(materialization.notes)
        state.add_risk_flags(materialization.warnings)
        state.add_note(
            f"Artifact materialization completed for {len(ordered_generated_files)} pending operations."
        )

        return state
