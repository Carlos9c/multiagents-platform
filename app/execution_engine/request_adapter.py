from __future__ import annotations

from sqlalchemy.orm import Session

from app.execution_engine.contracts import (
    ExecutionRequest,
    ProjectExecutionContext,
    RelatedTaskSummary,
)
from app.models.task import Task
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_storage import CODE_DOMAIN, ProjectStorageService


def _split_multiline_text(value: str | None) -> list[str]:
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def build_execution_request(
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
        acceptance_criteria=task.acceptance_criteria,
        technical_constraints=task.technical_constraints,
        out_of_scope=task.out_of_scope,
        executor_type=resolved_executor_type,
        success_criteria=_split_multiline_text(task.acceptance_criteria),
        constraints=_split_multiline_text(task.technical_constraints),
        allowed_paths=[],
        blocked_paths=[],
        context=context,
    )