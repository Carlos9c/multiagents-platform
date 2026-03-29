# app/services/tasks.py

from sqlalchemy.orm import Session

from app.models.task import (
    TASK_STATUS_AWAITING_VALIDATION,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_RUNNING,
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
