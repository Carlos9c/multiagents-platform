from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.execution_engine.context_selection import HistoricalTaskSelectionResult
from app.execution_engine.contracts import (
    ExecutionRequest,
    HistoricalExecutionContext,
    HistoricalTaskRunContext,
    ProjectExecutionContext,
    RelatedTaskSummary,
)
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.task import TASK_STATUS_FAILED, TASK_STATUS_PARTIAL, Task
from app.services.execution_runs import get_execution_run
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_memory_service import build_project_operational_context
from app.services.project_storage import ProjectStorageService


def _split_multiline_text(value: str | None) -> list[str]:
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def _split_semicolon_or_multiline_text(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = value.replace("\n", ";")
    return [item.strip() for item in normalized.split(";") if item.strip()]


def _safe_json_loads(raw: str | None) -> list | dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _deserialize_changed_files(raw: str | None) -> list[str]:
    payload = _safe_json_loads(raw)
    if not isinstance(payload, list):
        return []

    paths: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue

        path = item.get("path")
        if isinstance(path, str) and path.strip():
            paths.append(path.strip())

    return _dedupe_preserve_order(paths)


def _deserialize_files_read(raw: str | None) -> list[str]:
    """
    Deserialize persisted FileReadEvidence payloads into the simplified historical
    shape expected by HistoricalTaskRunContext.files_read: list[str].

    Current persisted shape:
    [
        {
            "path": "app/foo.py",
            "producer": "context_selection_agent",
            "source": "workspace"
        },
        ...
    ]

    Historical context intentionally keeps only paths.
    """
    payload = _safe_json_loads(raw)
    if not isinstance(payload, list):
        return []

    paths: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue

        path = item.get("path")
        if isinstance(path, str) and path.strip():
            paths.append(path.strip())

    return _dedupe_preserve_order(paths)


def _deserialize_change_dependencies(raw: str | None) -> dict[str, list[str]]:
    """
    Deserialize persisted ChangeDependencyEvidence payloads into the simplified
    historical shape expected by HistoricalTaskRunContext.change_dependencies:
    dict[path, list[depends_on_path]].

    Current persisted shape:
    [
        {
            "path": "app/foo.py",
            "depends_on": ["app/bar.py"],
            "producer": "code_change_agent"
        },
        ...
    ]

    If multiple producers reported dependencies for the same path, they are merged.
    """
    payload = _safe_json_loads(raw)
    if not isinstance(payload, list):
        return {}

    merged: dict[str, list[str]] = {}

    for item in payload:
        if not isinstance(item, dict):
            continue

        path = item.get("path")
        depends_on = item.get("depends_on")

        if not isinstance(path, str) or not path.strip():
            continue
        if not isinstance(depends_on, list):
            continue

        normalized_path = path.strip()
        normalized_deps = [
            dep.strip() for dep in depends_on if isinstance(dep, str) and dep.strip()
        ]

        if not normalized_deps:
            merged.setdefault(normalized_path, [])
            continue

        existing = merged.setdefault(normalized_path, [])
        merged[normalized_path] = _dedupe_preserve_order(existing + normalized_deps)

    return merged


def _load_dependency_file_contents(
    historical_context: HistoricalExecutionContext | None,
    *,
    source_path: str,
) -> dict[str, str]:
    if historical_context is None:
        return {}

    contents: dict[str, str] = {}
    seen: set[str] = set()
    source = Path(source_path)

    for run_ctx in historical_context.selected_task_runs:
        for rel_path in run_ctx.changed_files:
            if rel_path in seen:
                continue
            seen.add(rel_path)
            full_path = source / rel_path
            if full_path.is_file():
                try:
                    contents[rel_path] = full_path.read_text(encoding="utf-8")
                except Exception:
                    pass

    return contents


def _is_test_file(path: str) -> bool:
    lower = path.lower()
    return "/test" in lower or lower.startswith("test") or "\\test" in lower


def _load_recovery_source_file_hints(
    db: Session,
    *,
    task_id: int,
    source_path: str,
) -> dict[str, str]:
    """
    For recovery tasks, collect files_read from failed/partial sibling runs and
    return their contents so code_change_agent has the API surface without relying
    on context_selection_agent to pick the right historical tasks.
    """
    task = db.get(Task, task_id)
    if task is None:
        return {}

    followup_depth = getattr(task, "followup_depth", 0) or 0
    is_recovery = getattr(task, "is_recovery_task", False)

    if followup_depth == 0 and not is_recovery:
        return {}

    if task.parent_task_id is None:
        return {}

    sibling_tasks = (
        db.query(Task)
        .filter(
            Task.parent_task_id == task.parent_task_id,
            Task.id != task_id,
            Task.status.in_([TASK_STATUS_FAILED, TASK_STATUS_PARTIAL]),
        )
        .all()
    )

    if not sibling_tasks:
        return {}

    collected_paths: list[str] = []
    seen: set[str] = set()

    for sibling in sibling_tasks:
        latest_run = (
            db.query(ExecutionRun)
            .filter(ExecutionRun.task_id == sibling.id)
            .order_by(ExecutionRun.id.desc())
            .first()
        )
        if latest_run is None:
            continue
        for path in _deserialize_files_read(latest_run.files_read):
            if path not in seen:
                seen.add(path)
                collected_paths.append(path)

    non_test = [p for p in collected_paths if not _is_test_file(p)][:10]

    contents: dict[str, str] = {}
    source = Path(source_path)
    for rel_path in non_test:
        full_path = source / rel_path
        if full_path.is_file():
            try:
                contents[rel_path] = full_path.read_text(encoding="utf-8")
            except Exception:
                pass

    return contents


def _build_key_decisions(project_context) -> list[str]:
    return [item.summary for item in project_context.key_decisions if item.summary]


def _build_related_tasks(
    project_context,
    current_task_id: int,
) -> list[RelatedTaskSummary]:
    related: list[RelatedTaskSummary] = []

    for item in project_context.task_memory:
        if item.task_id == current_task_id:
            continue

        related.append(
            RelatedTaskSummary(
                task_id=item.task_id,
                title=item.title,
                status=item.status,
                summary=item.work_summary or item.objective,
            )
        )

    return related


def _build_historical_execution_context(
    db: Session,
    *,
    selection: HistoricalTaskSelectionResult | None,
) -> HistoricalExecutionContext | None:
    if selection is None or not selection.selected_task_runs:
        return None

    selected_task_runs: list[HistoricalTaskRunContext] = []

    for selected in selection.selected_task_runs:
        selected_task = db.get(Task, selected.task_id)
        if selected_task is None:
            continue

        selected_run = get_execution_run(db, selected.execution_run_id)
        if selected_run is None:
            continue

        if selected_run.task_id != selected.task_id:
            continue

        changed_files = _deserialize_changed_files(selected_run.changed_files)
        files_read = _deserialize_files_read(selected_run.files_read)
        change_dependencies = _deserialize_change_dependencies(selected_run.change_dependencies)

        selected_task_runs.append(
            HistoricalTaskRunContext(
                task_id=selected_task.id,
                execution_run_id=selected_run.id,
                selection_rule=selected.selection_rule,
                selection_reason=selected.selection_reason,
                title=selected_task.title,
                description=selected_task.description,
                summary=selected_task.summary,
                objective=selected_task.objective,
                run_summary=selected_run.work_summary,
                completed_scope=selected_run.completed_scope,
                validation_notes=_split_semicolon_or_multiline_text(selected_run.validation_notes),
                changed_files=changed_files,
                files_read=files_read,
                change_dependencies=change_dependencies,
            )
        )

    if not selected_task_runs:
        return None

    return HistoricalExecutionContext(selected_task_runs=selected_task_runs)


def build_placeholder_execution_request(
    db: Session,
    *,
    task: Task,
    execution_run_id: int,
    resolved_executor_type: str,
) -> ExecutionRequest:
    storage_service = ProjectStorageService()
    workspace_runtime = LocalWorkspaceRuntime(storage_service=storage_service)

    project_paths = storage_service.ensure_project_storage(task.project_id)
    workspace_paths = workspace_runtime.get_execution_workspace_paths(
        project_id=task.project_id,
        execution_run_id=execution_run_id,
    )

    sibling_tasks = (
        db.query(Task)
        .filter(Task.project_id == task.project_id, Task.id != task.id)
        .order_by(Task.id.asc())
        .limit(10)
        .all()
    )

    related_tasks = [
        RelatedTaskSummary(
            task_id=other.id,
            title=other.title,
            status=other.status,
            summary=other.summary,
        )
        for other in sibling_tasks
    ]

    project = db.get(Project, task.project_id)
    runtime_spec = project.runtime_spec if project else None

    context = ProjectExecutionContext(
        project_id=task.project_id,
        source_path=str(project_paths.source_dir),
        workspace_path=str(workspace_paths.workspace_dir),
        relevant_files=[],
        key_decisions=[],
        related_tasks=related_tasks,
        runtime_spec=runtime_spec,
    )

    return ExecutionRequest(
        task_id=task.id,
        project_id=task.project_id,
        execution_run_id=execution_run_id,
        task_title=task.title,
        task_description=task.description,
        task_summary=task.summary,
        objective=task.objective,
        proposed_solution=task.proposed_solution,
        implementation_notes=task.implementation_notes,
        implementation_steps=task.implementation_steps,
        acceptance_criteria=task.acceptance_criteria,
        tests_required=task.tests_required,
        technical_constraints=task.technical_constraints,
        out_of_scope=task.out_of_scope,
        task_type=task.task_type,
        executor_type=resolved_executor_type,
        verification_level=getattr(task, "verification_level", "runtime") or "runtime",
        success_criteria=_split_multiline_text(task.acceptance_criteria),
        constraints=_split_multiline_text(task.technical_constraints),
        allowed_paths=[],
        blocked_paths=[],
        context=context,
        historical_context=None,
    )


def adapt_execution_request(
    db: Session,
    *,
    request: ExecutionRequest,
    context_selection_result: HistoricalTaskSelectionResult | None,
) -> ExecutionRequest:
    project_context = build_project_operational_context(
        db=db,
        project_id=request.project_id,
    )

    historical_context = _build_historical_execution_context(
        db=db,
        selection=context_selection_result,
    )

    recovery_file_hints = _load_recovery_source_file_hints(
        db=db,
        task_id=request.task_id,
        source_path=request.context.source_path,
    )

    dependency_files = _load_dependency_file_contents(
        historical_context,
        source_path=request.context.source_path,
    )
    # Recovery hints are the fallback; dependency files from completed runs take priority.
    preloaded_dependency_files = {**recovery_file_hints, **dependency_files}

    adapted_context = ProjectExecutionContext(
        project_id=request.context.project_id,
        source_path=request.context.source_path,
        workspace_path=request.context.workspace_path,
        relevant_files=list(request.context.relevant_files),
        key_decisions=_build_key_decisions(project_context),
        related_tasks=_build_related_tasks(project_context, request.task_id),
        preloaded_dependency_files=preloaded_dependency_files,
        runtime_spec=request.context.runtime_spec,
    )

    return request.model_copy(
        update={
            "context": adapted_context,
            "historical_context": historical_context,
        }
    )
