from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.contracts import (
    CHANGE_TYPE_CREATED,
    CHANGE_TYPE_MODIFIED,
    ExecutionRequest,
)
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.file_operations import (
    FileMaterializationResult,
    MaterializedFile,
)
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.execution_engine.tools.file_reader_tool import read_text_file
from app.execution_engine.tools.file_snapshot_tool import (
    capture_file_snapshot,
    restore_file_snapshot,
)
from app.execution_engine.tools.file_writer_tool import write_text_file
from app.execution_engine.tools.workspace_scan_tool import list_workspace_files
from app.services.llm.schema_utils import to_openai_strict_json_schema

CODE_CHANGE_AGENT_SYSTEM_PROMPT = """
You are a senior artifact implementation agent.

Your job is to implement ONE already-atomic task by deciding which repository-relative
artifacts must be created or modified and by returning their full final contents.

Return ONLY JSON matching the provided schema.

Hard rules:
- Do not change the task.
- You may create and/or modify any files necessary to complete the task correctly.
- For operation=create, return the full final artifact content.
- For operation=modify, return the full updated artifact content.
- Use repository-relative paths only.
- Respect existing repository structure when it is the natural fit.
- Create new files when necessary.
- Do not invent broad unrelated refactors.
- Keep the implementation as small and coherent as possible.
- Use the provided project context and historical execution context when relevant.
- Do not validate final completion. Validation happens outside the execution engine.

Important execution scope rule:
- The provided files and workspace inventory are an initial context set, not a hard boundary.
- You may modify any additional files that are necessary to complete the task correctly and coherently.
- The objective is to complete the task fully, not merely to edit a predefined subset of files.

Operation integrity rules:
- Use modify for files that already exist in the project candidate baseline
  (either already present in the run overlay workspace or present in the persisted source baseline).
- Use create only for files that do not exist in either the run overlay workspace or the persisted source baseline.
- Do not return duplicate paths.
""".strip()


def _get_source_root_from_request(request: ExecutionRequest) -> str | None:
    context = request.context
    source_path = getattr(context, "source_path", None)
    if source_path:
        return str(source_path)

    source_dir = getattr(context, "source_dir", None)
    if source_dir:
        return str(source_dir)

    return None


def _resolve_candidate_file_for_read(
    *,
    workspace_root: str,
    source_root: str | None,
    relative_path: str,
) -> Path | None:
    workspace_candidate = (Path(workspace_root).resolve() / relative_path).resolve()
    workspace_root_path = Path(workspace_root).resolve()

    try:
        workspace_candidate.relative_to(workspace_root_path)
    except ValueError as exc:
        raise SubagentRejectedStepError(
            f"Refusing to read related file outside workspace boundary: {relative_path}"
        ) from exc

    if workspace_candidate.exists() and workspace_candidate.is_file():
        return workspace_candidate

    if source_root:
        source_root_path = Path(source_root).resolve()
        source_candidate = (source_root_path / relative_path).resolve()
        try:
            source_candidate.relative_to(source_root_path)
        except ValueError as exc:
            raise SubagentRejectedStepError(
                f"Refusing to read related file outside source boundary: {relative_path}"
            ) from exc

        if source_candidate.exists() and source_candidate.is_file():
            return source_candidate

    return None


def _candidate_file_exists(
    *,
    workspace_root: str,
    source_root: str | None,
    relative_path: str,
) -> bool:
    return (
        _resolve_candidate_file_for_read(
            workspace_root=workspace_root,
            source_root=source_root,
            relative_path=relative_path,
        )
        is not None
    )


def _format_evidence_notes_for_prompt(state: ResolutionState) -> list[str]:
    messages: list[str] = []

    for note in state.evidence.notes:
        message = getattr(note, "message", None)
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())

    return messages


def _build_historical_context_summary(request: ExecutionRequest) -> str:
    historical_context = request.historical_context
    if historical_context is None or not historical_context.selected_task_runs:
        return "[no historical task context available]"

    parts: list[str] = []
    for item in historical_context.selected_task_runs:
        parts.append(f"- task_id: {item.task_id}")
        parts.append(f"  execution_run_id: {item.execution_run_id}")
        parts.append(f"  title: {item.title}")
        parts.append(f"  selection_rule: {item.selection_rule}")
        parts.append(f"  selection_reason: {item.selection_reason}")
        parts.append(f"  summary: {item.summary}")
        parts.append(f"  objective: {item.objective}")
        parts.append(f"  run_summary: {item.run_summary}")
        parts.append(f"  completed_scope: {item.completed_scope}")
        parts.append(f"  validation_notes: {item.validation_notes}")
        parts.append(f"  changed_files: {item.changed_files}")
        parts.append(f"  files_read: {item.files_read}")
        parts.append(f"  change_dependencies: {item.change_dependencies}")

    return "\n".join(parts)


def _build_project_context_summary(request: ExecutionRequest) -> str:
    related = [
        {
            "task_id": item.task_id,
            "title": item.title,
            "status": item.status,
            "summary": item.summary,
        }
        for item in request.context.related_tasks
    ]

    source_root = _get_source_root_from_request(request)

    return f"""
- relevant_files: {request.context.relevant_files}
- key_decisions: {request.context.key_decisions}
- related_tasks: {related}
- allowed_paths: {request.allowed_paths}
- blocked_paths: {request.blocked_paths}
- workspace_overlay_root: {request.context.workspace_path}
- source_baseline_root: {source_root}
""".strip()


def _build_workspace_inventory_context(
    *,
    workspace_root: str,
    source_root: str | None,
    max_files: int = 500,
) -> str:
    overlay_files = list_workspace_files(workspace_root, max_files=max_files)

    baseline_files: list[str] = []
    if source_root:
        baseline_root = Path(source_root).resolve()
        if baseline_root.exists():
            for path in baseline_root.rglob("*"):
                if path.is_file():
                    baseline_files.append(path.relative_to(baseline_root).as_posix())
            baseline_files = sorted(baseline_files)[:max_files]

    overlay_section = (
        "\n".join(f"- {path}" for path in overlay_files)
        if overlay_files
        else "[workspace overlay is currently empty]"
    )
    baseline_section = (
        "\n".join(f"- {path}" for path in baseline_files)
        if baseline_files
        else "[source baseline is currently empty]"
    )

    return (
        "Workspace overlay inventory:\n"
        f"{overlay_section}\n\n"
        "Source baseline inventory:\n"
        f"{baseline_section}"
    )


def _build_related_file_context(
    *,
    workspace_root: str,
    source_root: str | None,
    request: ExecutionRequest,
    max_files: int = 12,
) -> tuple[str, list[tuple[str, str | None]]]:
    candidates: list[str] = []

    candidates.extend(request.context.relevant_files)

    historical_context = request.historical_context
    if historical_context is not None:
        for item in historical_context.selected_task_runs:
            candidates.extend(item.changed_files)
            candidates.extend(item.files_read)

    seen: set[str] = set()
    selected: list[str] = []
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        selected.append(path)

    selected = selected[:max_files]

    if not selected:
        return "[no related file content loaded]", []

    parts: list[str] = []
    files_read: list[tuple[str, str | None]] = []

    for rel_path in selected:
        parts.append(f"- path: {rel_path}")

        resolved = _resolve_candidate_file_for_read(
            workspace_root=workspace_root,
            source_root=source_root,
            relative_path=rel_path,
        )

        if resolved is None:
            parts.append("  source: [missing file]")
            parts.append("  content: [missing file]")
            continue

        try:
            content = read_text_file(str(resolved))

            source_label = "workspace_overlay"
            if source_root:
                source_candidate = (Path(source_root).resolve() / rel_path).resolve()
                if resolved == source_candidate:
                    source_label = "source_baseline"

            files_read.append((rel_path, source_label))
            parts.append(f"  source: {source_label}")
            parts.append("  content:")
            parts.append(content)
        except Exception as exc:
            parts.append(f"  content_error: {str(exc)}")

    return "\n".join(parts), files_read


def _build_user_prompt(
    request: ExecutionRequest,
    state: ResolutionState,
) -> tuple[str, list[tuple[str, str | None]]]:
    source_root = _get_source_root_from_request(request)

    workspace_inventory = _build_workspace_inventory_context(
        workspace_root=request.context.workspace_path,
        source_root=source_root,
    )
    related_file_context, files_read = _build_related_file_context(
        workspace_root=request.context.workspace_path,
        source_root=source_root,
        request=request,
    )

    project_context_summary = _build_project_context_summary(request)
    historical_context_summary = _build_historical_context_summary(request)
    evidence_notes = _format_evidence_notes_for_prompt(state)

    prompt = f"""
Task:
- task_id: {request.task_id}
- title: {request.task_title}
- description: {request.task_description}
- summary: {request.task_summary}
- objective: {request.objective}
- proposed_solution: {request.proposed_solution}
- implementation_notes: {request.implementation_notes}
- implementation_steps: {request.implementation_steps}
- acceptance_criteria: {request.acceptance_criteria}
- tests_required: {request.tests_required}
- technical_constraints: {request.technical_constraints}
- out_of_scope: {request.out_of_scope}
- executor_type: {request.executor_type}

Project context:
{project_context_summary}

Historical task context:
{historical_context_summary}

Repository inventory:
{workspace_inventory}

Related file content:
{related_file_context}

Current orchestration state:
- phase: {state.phase}
- materialization_attempt_count: {state.materialization_attempt_count}
- risk_flags: {state.risk_flags}
- step_notes: {state.step_notes}
- evidence_notes: {evidence_notes}

Important:
- The workspace is an editable overlay for this execution run.
- The source baseline is the persisted project tree.
- When deciding create vs modify, reason against the project candidate baseline:
  workspace overlay takes precedence, then source baseline.
- If the overlay is empty, bootstrap the minimal coherent file set required by the task.
- The listed files are initial context, not a hard boundary.
- Prefer completeness and coherence over artificial file limits.
- Keep the implementation conservative and scoped.
""".strip()

    return prompt, files_read


def _validate_generated_files(
    *,
    workspace_root: str,
    source_root: str | None,
    files: list[MaterializedFile],
) -> None:
    if not files:
        raise SubagentRejectedStepError("Code change agent returned no files to materialize.")

    root = Path(workspace_root).resolve()
    seen_paths: set[str] = set()

    for item in files:
        if not item.path or not item.path.strip():
            raise SubagentRejectedStepError("Code change agent returned an empty file path.")

        rel_path = item.path.strip()
        if rel_path in seen_paths:
            raise SubagentRejectedStepError(
                f"Duplicate file path returned by code change agent: {rel_path}"
            )
        seen_paths.add(rel_path)

        destination = (root / rel_path).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise SubagentRejectedStepError(
                f"Refusing to materialize file outside workspace root: {rel_path}"
            ) from exc

        exists_in_candidate_baseline = _candidate_file_exists(
            workspace_root=workspace_root,
            source_root=source_root,
            relative_path=rel_path,
        )

        if item.operation == "modify" and not exists_in_candidate_baseline:
            raise SubagentRejectedStepError(
                f"File '{rel_path}' does not exist in the project candidate baseline, "
                "so operation must be 'create' instead of 'modify'."
            )

        if item.operation == "create" and exists_in_candidate_baseline:
            raise SubagentRejectedStepError(
                f"File '{rel_path}' already exists in the project candidate baseline, "
                "so operation must be 'modify' instead of 'create'."
            )


class CodeChangeAgent(BaseSubagent):
    name = "code_change_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self.runtime = runtime

    def execute_step(
        self,
        *,
        db: Session,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        if step.subagent_name != self.name:
            raise SubagentRejectedStepError(
                f"{self.name} received a step for subagent '{step.subagent_name}'."
            )

        state.increment_materialization_attempts()

        source_root = _get_source_root_from_request(request)

        user_prompt, files_read = _build_user_prompt(request, state)
        for path, source_label in files_read:
            state.evidence.add_file_read(
                path=path,
                producer=self.name,
                source=source_label,
            )

        schema = to_openai_strict_json_schema(FileMaterializationResult.model_json_schema())
        raw = self.runtime.generate_structured(
            system_prompt=CODE_CHANGE_AGENT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="execution_engine_file_materialization",
            json_schema=schema,
        )

        try:
            materialization = FileMaterializationResult.model_validate(raw)
        except ValidationError as exc:
            raise SubagentRejectedStepError(f"Invalid code change output: {str(exc)}") from exc

        _validate_generated_files(
            workspace_root=request.context.workspace_path,
            source_root=source_root,
            files=materialization.files,
        )

        ordered_generated_files = sorted(
            materialization.files,
            key=lambda item: (item.path, item.operation),
        )

        snapshot_paths = [item.path for item in ordered_generated_files]
        snapshots = capture_file_snapshot(
            root_dir=request.context.workspace_path,
            relative_paths=snapshot_paths,
        )

        try:
            for generated in ordered_generated_files:
                absolute_path = write_text_file(
                    root_dir=request.context.workspace_path,
                    relative_path=generated.path,
                    content=generated.content,
                )

                change_type = (
                    CHANGE_TYPE_CREATED if generated.operation == "create" else CHANGE_TYPE_MODIFIED
                )

                state.evidence.add_changed_file(
                    path=generated.path,
                    change_type=change_type,
                    producer=self.name,
                )
                state.evidence.add_note(
                    message=f"Wrote file {generated.path} at {absolute_path}",
                    producer=self.name,
                )
                state.evidence.add_note(
                    message=f"Rationale for {generated.path}: {generated.rationale}",
                    producer=self.name,
                )

        except Exception:
            restore_file_snapshot(
                root_dir=request.context.workspace_path,
                snapshots=snapshots,
            )
            raise

        for note in materialization.notes:
            state.evidence.add_note(
                message=note,
                producer=self.name,
            )

        state.add_risk_flags(materialization.warnings)
        state.add_note(
            f"Artifact materialization completed for {len(ordered_generated_files)} files."
        )

        return state
