from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.execution_engine.context_selection import (
    ContextBuilderResult,
    HistoricalTaskCatalogEntry,
)
from app.models.task import TASK_STATUS_COMPLETED, Task
from app.services.execution_runs import get_completion_execution_run_for_task
from app.services.project_memory_service import build_project_operational_context


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


def _build_project_context_excerpt(project_context) -> str | None:
    lines: list[str] = []

    if project_context.summary:
        lines.append(project_context.summary)

    if project_context.key_decisions:
        decisions = [
            f"- {item.summary}"
            for item in project_context.key_decisions[:8]
            if item.summary
        ]
        if decisions:
            lines.append("Key decisions:\n" + "\n".join(decisions))

    if project_context.recent_completed_work:
        lines.append(
            "Recent completed work:\n"
            + "\n".join(f"- {item}" for item in project_context.recent_completed_work[:8])
        )

    if project_context.referenced_paths:
        lines.append(
            "Referenced paths:\n"
            + "\n".join(
                f"- {item.path} (mentions={item.mention_count})"
                for item in project_context.referenced_paths[:12]
                if item.path
            )
        )

    if project_context.open_gaps:
        lines.append(
            "Open gaps:\n"
            + "\n".join(f"- {item}" for item in project_context.open_gaps[:8])
        )

    excerpt = "\n\n".join(line for line in lines if line.strip())
    return excerpt or None


def _build_completed_task_catalog(
    db: Session,
    *,
    project_id: int,
    exclude_task_id: int | None = None,
) -> list[HistoricalTaskCatalogEntry]:
    query = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.status == TASK_STATUS_COMPLETED,
        )
        .order_by(Task.id.asc())
    )

    if exclude_task_id is not None:
        query = query.filter(Task.id != exclude_task_id)

    completed_tasks = query.all()

    catalog: list[HistoricalTaskCatalogEntry] = []

    for task in completed_tasks:
        completion_run = get_completion_execution_run_for_task(db, task.id)
        if completion_run is None:
            continue

        catalog.append(
            HistoricalTaskCatalogEntry(
                task_id=task.id,
                execution_run_id=completion_run.id,
                title=task.title,
                description=task.description,
                summary=task.summary,
                objective=task.objective,
                task_type=task.task_type,
                executor_type=task.executor_type,
                run_summary=completion_run.work_summary,
                completed_scope=completion_run.completed_scope,
                validation_notes=completion_run.validation_notes,
                changed_files=_deserialize_changed_files(
                    completion_run.changed_files
                ),
                files_read=_deserialize_string_list(
                    completion_run.files_read
                ),
            )
        )

    return catalog


def build_context_selection_input(
    db: Session,
    *,
    current_task: Task,
) -> ContextBuilderResult:
    completed_task_catalog = _build_completed_task_catalog(
        db,
        project_id=current_task.project_id,
        exclude_task_id=current_task.id,
    )

    project_context = build_project_operational_context(
        db=db,
        project_id=current_task.project_id,
    )
    project_context_excerpt = _build_project_context_excerpt(project_context)

    if not completed_task_catalog:
        return ContextBuilderResult(
            should_invoke_context_selection_agent=False,
            completed_task_catalog=[],
            project_context_excerpt=project_context_excerpt,
        )

    return ContextBuilderResult(
        should_invoke_context_selection_agent=True,
        completed_task_catalog=completed_task_catalog,
        project_context_excerpt=project_context_excerpt,
    )