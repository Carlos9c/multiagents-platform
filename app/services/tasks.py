# app/services/tasks.py


from sqlalchemy.orm import Session

from app.models.task import (
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_AWAITING_VALIDATION,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_REATOMIZED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUPERSEDED,
    Task,
)


def _finalize_persistence(
    db: Session,
    *,
    entity,
    auto_commit: bool,
) -> None:
    if auto_commit:
        db.commit()
        db.refresh(entity)
    else:
        db.flush()


def mark_task_running(
    db: Session,
    task_id: int,
    auto_commit: bool = True,
) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_RUNNING
    _finalize_persistence(db, entity=task, auto_commit=auto_commit)
    return task


def mark_task_awaiting_validation(
    db: Session,
    task_id: int,
    auto_commit: bool = True,
) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_AWAITING_VALIDATION
    _finalize_persistence(db, entity=task, auto_commit=auto_commit)
    return task


def mark_task_partial(
    db: Session,
    task_id: int,
    auto_commit: bool = True,
) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_PARTIAL
    _finalize_persistence(db, entity=task, auto_commit=auto_commit)
    return task


def mark_task_completed(
    db: Session,
    task_id: int,
    auto_commit: bool = True,
) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_COMPLETED
    _finalize_persistence(db, entity=task, auto_commit=auto_commit)
    return task


def mark_task_failed(
    db: Session,
    task_id: int,
    auto_commit: bool = True,
) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_FAILED
    _finalize_persistence(db, entity=task, auto_commit=auto_commit)
    return task


def mark_task_reatomized(
    db: Session,
    task_id: int,
    auto_commit: bool = True,
) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_REATOMIZED
    _finalize_persistence(db, entity=task, auto_commit=auto_commit)
    return task


def mark_task_awaiting_review(
    db: Session,
    task_id: int,
    auto_commit: bool = True,
) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_AWAITING_REVIEW
    task.review_attempts = (task.review_attempts or 0) + 1
    _finalize_persistence(db, entity=task, auto_commit=auto_commit)
    return task


def mark_tasks_superseded(
    db: Session,
    task_ids: list[int],
    auto_commit: bool = True,
) -> int:
    """Bulk-mark tasks as superseded. Returns the count of updated tasks."""
    if not task_ids:
        return 0

    updated = (
        db.query(Task)
        .filter(Task.id.in_(task_ids))
        .all()
    )
    for task in updated:
        task.status = TASK_STATUS_SUPERSEDED

    if auto_commit:
        db.commit()
    else:
        db.flush()

    return len(updated)
