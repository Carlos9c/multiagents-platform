from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

DOCUMENT_WRITER_AGENT_SYSTEM_PROMPT = """
You are a senior documentation and design agent.

Your job is to produce documentation, design artifacts, and specification deliverables
for ONE already-atomic task by deciding which repository-relative files must be created
or modified and by returning their full final contents.

Core responsibility:
- Produce complete, substantive artifacts — never skeletons, never placeholders.
- Every section of every document must contain real content derived from the task context.
- Preserve and extend the existing documentation structure when one already exists.
- Add new files only when they are truly needed.
- Avoid repeating content already covered by referenced historical artifacts.

Supported output formats:
- Markdown (.md): README, architecture docs, design decisions (ADRs), contributor guides,
  API docs, changelogs, onboarding guides, feature specs.
- YAML/JSON (.yaml, .yml, .json): OpenAPI/AsyncAPI specs, schema definitions, structured configs.
- reStructuredText (.rst), AsciiDoc (.adoc), plain text (.txt).
- Diagram-as-code (.puml, .mmd, .drawio): PlantUML, Mermaid, draw.io XML.
- Any other plain-text format the task explicitly requires.

Hard rules:
- Do not change the task scope.
- For operation=create, return the full final artifact content.
- For operation=modify, return the full updated artifact content — do not omit existing sections.
- Use repository-relative paths only.
- Produce only text artifacts: documentation, design documents, specifications, README files, API docs, guides.
  You may include code snippets inside those documents as examples, but do not produce standalone source code
  files (.py, .ts, .kt, .java, etc.) that are meant to be executed or imported — those belong to code_change_agent.
- Do not produce compiled or binary outputs.
- Every document must stand on its own: a reader unfamiliar with the task should
  find the content complete and self-explanatory within its stated scope.

Operation integrity rules:
- Use modify for files that already exist in the project candidate baseline.
- Use create only for files that do not exist in either the run overlay workspace
  or the persisted source baseline.
- Do not return duplicate paths.

Content quality expectations:
- Match the dominant style and structure already present in the repository's existing docs.
- Populate every heading, section, and table with concrete content.
- When writing design or architecture documents, derive decisions and rationale from the
  task description, objective, proposed solution, and acceptance criteria.
- When writing API or schema documents, derive field names, types, and descriptions from the
  task technical constraints and implementation context.
- When writing guides or onboarding docs, write procedurally — steps must be executable.
- Do not add speculative sections beyond what the task requires.
- Do not include TODO, FIXME, or placeholder markers in the output.

Decision policy:
- First decide the minimal coherent artifact set that satisfies the task.
- Then ensure all returned files form one consistent, readable documentation state.
- Optimize for completeness, accuracy, and readability — not for brevity or novelty.
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
            f"Refusing to read file outside workspace boundary: {relative_path}"
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
                f"Refusing to read file outside source boundary: {relative_path}"
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


def _build_historical_context_summary(request: ExecutionRequest) -> str:
    historical_context = request.historical_context
    if historical_context is None or not historical_context.selected_task_runs:
        return "[no historical task context available]"

    parts: list[str] = []
    for item in historical_context.selected_task_runs:
        parts.append(f"- task_id: {item.task_id}")
        parts.append(f"  title: {item.title}")
        parts.append(f"  selection_rule: {item.selection_rule}")
        parts.append(f"  selection_reason: {item.selection_reason}")
        parts.append(f"  summary: {item.summary}")
        parts.append(f"  objective: {item.objective}")
        parts.append(f"  run_summary: {item.run_summary}")
        parts.append(f"  completed_scope: {item.completed_scope}")
        parts.append(f"  changed_files: {item.changed_files}")
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
    max_files: int = 8,
) -> tuple[str, list[tuple[str, str | None]]]:
    preloaded = request.context.preloaded_dependency_files

    parts: list[str] = []
    files_read: list[tuple[str, str | None]] = []

    for rel_path, content in preloaded.items():
        parts.append(f"- path: {rel_path}")
        parts.append("  source: preloaded_dependency")
        parts.append("  content:")
        parts.append(content)
        files_read.append((rel_path, "preloaded_dependency"))

    candidates: list[str] = list(request.context.relevant_files)
    historical_context = request.historical_context
    if historical_context is not None:
        for item in historical_context.selected_task_runs:
            candidates.extend(item.changed_files)

    seen: set[str] = set(preloaded.keys())
    selected: list[str] = []
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        selected.append(path)
    selected = selected[:max_files]

    if not selected and not parts:
        return "[no related file content loaded]", []

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
    step: ExecutionStep,
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

Current subagent step:
- step_id: {step.id}
- step_title: {step.title}
- step_instructions: {step.instructions}
- target_paths: {step.target_paths}

Project context:
{project_context_summary}

Historical task context:
{historical_context_summary}

Repository inventory:
{workspace_inventory}

Related file content (for style reference and avoiding duplication):
{related_file_context}

Quality expectations:
- Every returned file must contain complete, substantive content — no placeholders or TODOs.
- Derive all content from the task description, objective, proposed solution, and technical constraints.
- Match the existing documentation style and structure present in the repository.
- When deciding create vs modify, reason against the project candidate baseline:
  workspace overlay takes precedence, then source baseline.
- Produce only documentation and design artifacts. You may embed code snippets inside documents as examples,
  but do not produce standalone source code files meant to be executed or imported.
""".strip()

    return prompt, files_read


def _validate_generated_files(
    *,
    workspace_root: str,
    source_root: str | None,
    files: list[MaterializedFile],
) -> None:
    if not files:
        raise SubagentRejectedStepError("Document writer agent returned no files to materialize.")

    root = Path(workspace_root).resolve()
    seen_paths: set[str] = set()

    for item in files:
        if not item.path or not item.path.strip():
            raise SubagentRejectedStepError("Document writer agent returned an empty file path.")

        rel_path = item.path.strip()
        if rel_path in seen_paths:
            raise SubagentRejectedStepError(
                f"Duplicate file path returned by document writer agent: {rel_path}"
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


class DocumentWriterAgent(BaseSubagent):
    name = "document_writer_agent"

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

        logger.info(
            "document_writer_agent_started task_id=%s step_id=%s attempt=%s",
            request.task_id,
            step.id,
            state.materialization_attempt_count,
        )

        source_root = _get_source_root_from_request(request)

        user_prompt, files_read = _build_user_prompt(request, step, state)
        for path, source_label in files_read:
            state.evidence.add_file_read(
                path=path,
                producer=self.name,
                source=source_label,
            )

        raw = self.runtime.generate_structured(
            system_prompt=DOCUMENT_WRITER_AGENT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="document_writer_agent_materialization",
            json_schema=FileMaterializationResult.model_json_schema(),
        )

        try:
            materialization = FileMaterializationResult.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "document_writer_agent_invalid_output task_id=%s step_id=%s error=%s",
                request.task_id,
                step.id,
                str(exc),
            )
            raise SubagentRejectedStepError(
                f"Invalid document writer output: {str(exc)}"
            ) from exc

        logger.info(
            "document_writer_agent_files_planned task_id=%s step_id=%s file_count=%s",
            request.task_id,
            step.id,
            len(materialization.files),
        )

        _validate_generated_files(
            workspace_root=request.context.workspace_path,
            source_root=source_root,
            files=materialization.files,
        )

        ordered_files = sorted(
            materialization.files,
            key=lambda item: (item.path, item.operation),
        )

        snapshot_paths = [item.path for item in ordered_files]
        snapshots = capture_file_snapshot(
            root_dir=request.context.workspace_path,
            relative_paths=snapshot_paths,
        )

        try:
            for generated in ordered_files:
                absolute_path = write_text_file(
                    root_dir=request.context.workspace_path,
                    relative_path=generated.path,
                    content=generated.content,
                )

                change_type = (
                    CHANGE_TYPE_CREATED if generated.operation == "create" else CHANGE_TYPE_MODIFIED
                )

                logger.info(
                    "document_writer_agent_file_written task_id=%s path=%s operation=%s",
                    request.task_id,
                    generated.path,
                    generated.operation,
                )

                state.evidence.add_changed_file(
                    path=generated.path,
                    change_type=change_type,
                    producer=self.name,
                )
                state.evidence.add_note(
                    message=f"Wrote document {generated.path} at {absolute_path}",
                    producer=self.name,
                )
                state.evidence.add_note(
                    message=f"Rationale for {generated.path}: {generated.rationale}",
                    producer=self.name,
                )

        except Exception:
            logger.exception(
                "document_writer_agent_write_failed task_id=%s step_id=%s rolling_back=true",
                request.task_id,
                step.id,
            )
            restore_file_snapshot(
                root_dir=request.context.workspace_path,
                snapshots=snapshots,
            )
            raise

        for note in materialization.notes:
            state.evidence.add_note(message=note, producer=self.name)

        state.add_risk_flags(materialization.warnings)
        state.add_note(
            f"Document materialization completed for {len(ordered_files)} file(s)."
        )

        logger.info(
            "document_writer_agent_completed task_id=%s step_id=%s files_written=%s",
            request.task_id,
            step.id,
            len(ordered_files),
        )

        return state
