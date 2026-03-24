from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.task import Task
from app.services.task_hierarchy_service import (
    TaskHierarchyServiceError,
    consolidate_parent_task_statuses,
)


class TaskHierarchyReconciliationServiceError(Exception):
    """Raised when affected task hierarchy reconciliation fails."""


def _get_existing_tasks(
    db: Session,
    *,
    task_ids: list[int],
) -> list[Task]:
    if not task_ids:
        return []

    return (
        db.query(Task)
        .filter(Task.id.in_(task_ids))
        .order_by(Task.id.asc())
        .all()
    )


def _collect_affected_parent_ids(tasks: list[Task]) -> list[int]:
    parent_ids = {
        task.parent_task_id
        for task in tasks
        if task.parent_task_id is not None
    }
    return sorted(parent_ids)


def reconcile_task_hierarchy_after_changes(
    db: Session,
    *,
    affected_task_ids: list[int],
) -> list[int]:
    """
    Reconcile the deterministic parent hierarchy for every parent impacted by the
    supplied task ids.

    Notes:
    - Task ids that do not exist anymore are ignored.
    - Only direct parents are collected here; upward propagation is handled by
      consolidate_parent_task_statuses().
    - Returns the parent ids that were considered for reconciliation.
    """
    tasks = _get_existing_tasks(
        db=db,
        task_ids=list(dict.fromkeys(affected_task_ids)),
    )
    affected_parent_ids = _collect_affected_parent_ids(tasks)

    try:
        for parent_task_id in affected_parent_ids:
            consolidate_parent_task_statuses(
                db=db,
                parent_task_id=parent_task_id,
            )
    except TaskHierarchyServiceError as exc:
        raise TaskHierarchyReconciliationServiceError(
            f"Failed to reconcile affected parent hierarchies: {str(exc)}"
        ) from exc

    return affected_parent_ids