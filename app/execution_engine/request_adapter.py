from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.execution_engine.context_selection import HistoricalTaskSelectionResult
from app.execution_engine.contracts import (
    ExecutionRequest,
    HistoricalExecutionContext,
    HistoricalTaskRunContext,
    ProjectExecutionContext,
    RelatedTaskSummary,
)
from app.models.task import Task
from app.services.execution_runs import get_execution_run
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_memory_service import build_project_operational_context
from app.services.project_storage import CODE_DOMAIN, ProjectStorageService


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


def _deserialize_changed_files(raw: str | None) -> list[str]:
    payload = _safe_json_loads(raw)
    if not isinstance(payload, list):
        return []

    paths: list[str] = []
    for item in payload:
        if isinstance(item, dict):
            path = item.get("path")
            if isinstance(path, str) and path.strip():
                paths.append(path.strip())

    return list(dict.fromkeys(paths))


def _deserialize_string_list(raw: str | None) -> list[str]:
    payload = _safe_json_loads(raw)
    if not isinstance(payload, list):
        return []

    values: list[str] = []
    for item in payload:
        if isinstance(item, str) and item.strip():
            values.append(item.strip())

    return list(dict.fromkeys(values))


def _deserialize_change_dependencies(raw: str | None) -> dict[str, list[str]]:
    payload = _safe_json_loads(raw)
    if not isinstance(payload, dict):
        return {}

    result: dict[str, list[str]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(value, list):
            continue

        deps: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                deps.append(item.strip())

        result[key.strip()] = list(dict.fromkeys(deps))

    return result


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


def _build_relevant_files(
    *,
    project_context,
    historical_context: HistoricalExecutionContext | None,
) -> list[str]:
    paths: list[str] = []

    for item in project_context.referenced_paths:
        if item.path:
            paths.append(item.path)

    if historical_context is not None:
        for selected in historical_context.selected_task_runs:
            paths.extend(selected.changed_files)
            paths.extend(selected.files_read)

    return list(dict.fromkeys(path for path in paths if path))


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
        files_read = _deserialize_string_list(selected_run.files_read)
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

    domain_paths = storage_service.ensure_domain_storage(
        project_id=task.project_id,
        domain_name=CODE_DOMAIN,
    )
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

    context = ProjectExecutionContext(
        project_id=task.project_id,
        source_path=str(domain_paths.source_dir) if domain_paths.source_dir else "",
        workspace_path=str(workspace_paths.workspace_dir),
        relevant_files=[],
        key_decisions=[],
        related_tasks=related_tasks,
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
        executor_type=resolved_executor_type,
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

    adapted_context = ProjectExecutionContext(
        project_id=request.context.project_id,
        source_path=request.context.source_path,
        workspace_path=request.context.workspace_path,
        relevant_files=_build_relevant_files(
            project_context=project_context,
            historical_context=historical_context,
        ),
        key_decisions=_build_key_decisions(project_context),
        related_tasks=_build_related_tasks(project_context, request.task_id),
    )

    return request.model_copy(
        update={
            "success_criteria": _split_multiline_text(request.acceptance_criteria),
            "constraints": _split_multiline_text(request.technical_constraints),
            "context": adapted_context,
            "historical_context": historical_context,
        }
    )
