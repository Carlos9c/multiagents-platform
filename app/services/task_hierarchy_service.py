from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_FOLLOWED_UP,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TASK_STATUS_REATOMIZED,
    TERMINAL_TASK_STATUSES,
    Task,
)


class TaskHierarchyServiceError(Exception):
    """Base exception for deterministic task hierarchy consolidation."""


@dataclass
class ParentConsolidationChange:
    task_id: int
    previous_status: str
    new_status: str


@dataclass
class ParentConsolidationResult:
    starting_parent_task_id: int | None
    changed_task_ids: list[int]
    changes: list[ParentConsolidationChange]


def _get_task_or_raise(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise TaskHierarchyServiceError(f"Task {task_id} not found")
    return task


def _get_children(db: Session, parent_task_id: int) -> list[Task]:
    return (
        db.query(Task).filter(Task.parent_task_id == parent_task_id).order_by(Task.id.asc()).all()
    )


def _is_terminal_status(status: str) -> bool:
    return status in TERMINAL_TASK_STATUSES


def _derive_parent_status_from_children(children: list[Task]) -> str | None:
    """
    Deterministic parent status resolution.

    Rules (evaluated in order):
    - If parent has no children → do not change it.
    - If any child is non-terminal → parent = pending
    - If any child is failed → parent = failed
    - If any child is partial → parent = partial
    - If any child has productive terminal status (completed / reatomized / followed_up)
      → parent = completed.  Remaining children may be cancelled or superseded — those
      represent work the user explicitly discarded, not execution failures, so they do
      not block the parent from being recognised as complete.
    - Else → parent = pending  (all children are cancelled / superseded; the cascade
      logic in _cancel_tasks_and_cascade() normally sets the parent directly in this
      case, so this branch is a safety fallthrough).

    Important semantic rule:
    'failed' and 'partial' are aggregate terminal states of the parent and must
    only be assigned when there are no remaining non-terminal children.
    """
    if not children:
        return None

    child_statuses = [child.status for child in children]

    # 1. Any non-terminal child — parent is still in progress
    if any(not _is_terminal_status(status) for status in child_statuses):
        return TASK_STATUS_PENDING

    # 2. Execution failures override everything
    if any(status == TASK_STATUS_FAILED for status in child_statuses):
        return TASK_STATUS_FAILED

    # 3. Partial execution — work started but incomplete
    if any(status == TASK_STATUS_PARTIAL for status in child_statuses):
        return TASK_STATUS_PARTIAL

    # 4. At least one child has productive terminal status.
    #    Cancelled / superseded siblings do not block parent completion — they
    #    represent user decisions (cancelled) or plan replacements (superseded).
    _PRODUCTIVE_TERMINAL = {TASK_STATUS_COMPLETED, TASK_STATUS_REATOMIZED, TASK_STATUS_FOLLOWED_UP}
    if any(status in _PRODUCTIVE_TERMINAL for status in child_statuses):
        return TASK_STATUS_COMPLETED

    # 5. All children are cancelled / superseded — safety fallthrough.
    #    _cancel_tasks_and_cascade() already sets the parent in this case.
    return TASK_STATUS_PENDING


def _consolidate_single_parent(
    db: Session,
    parent_task: Task,
) -> ParentConsolidationChange | None:
    if parent_task.planning_level == PLANNING_LEVEL_ATOMIC:
        raise TaskHierarchyServiceError(
            f"Task {parent_task.id} is atomic and cannot be consolidated as a parent task."
        )

    children = _get_children(db, parent_task.id)
    derived_status = _derive_parent_status_from_children(children)

    if derived_status is None:
        return None

    previous_status = parent_task.status
    if previous_status == derived_status:
        return None

    parent_task.status = derived_status
    db.flush()

    return ParentConsolidationChange(
        task_id=parent_task.id,
        previous_status=previous_status,
        new_status=derived_status,
    )


def consolidate_parent_task_statuses(
    db: Session,
    *,
    task_id: int | None = None,
    parent_task_id: int | None = None,
    auto_commit: bool = True,
) -> ParentConsolidationResult:
    """
    Recompute and propagate deterministic status consolidation up the hierarchy.

    Usage:
    - task_id=<child task id> to start from its parent
    - parent_task_id=<direct parent id> when caller already knows it
    """
    if task_id is None and parent_task_id is None:
        raise TaskHierarchyServiceError(
            "consolidate_parent_task_statuses() requires either task_id or parent_task_id."
        )

    starting_parent_task_id = parent_task_id

    if parent_task_id is None:
        task = _get_task_or_raise(db, task_id)
        current_parent_id = task.parent_task_id
        starting_parent_task_id = current_parent_id
    else:
        current_parent_id = parent_task_id

    changes: list[ParentConsolidationChange] = []

    while current_parent_id is not None:
        parent_task = _get_task_or_raise(db, current_parent_id)
        change = _consolidate_single_parent(db, parent_task)

        if change is not None:
            changes.append(change)

        current_parent_id = parent_task.parent_task_id

    if auto_commit:
        db.commit()
    else:
        db.flush()

    return ParentConsolidationResult(
        starting_parent_task_id=starting_parent_task_id,
        changed_task_ids=[change.task_id for change in changes],
        changes=changes,
    )
