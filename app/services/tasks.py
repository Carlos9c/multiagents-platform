from sqlalchemy.orm import Session

from app.models.task import (
    TASK_STATUS_AWAITING_VALIDATION,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_RUNNING,
    Task,
)


def mark_task_running(db: Session, task_id: int) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_RUNNING
    db.commit()
    db.refresh(task)
    return task


def mark_task_awaiting_validation(db: Session, task_id: int) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_AWAITING_VALIDATION
    db.commit()
    db.refresh(task)
    return task


def mark_task_partial(db: Session, task_id: int) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_PARTIAL
    db.commit()
    db.refresh(task)
    return task


def mark_task_completed(db: Session, task_id: int) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_COMPLETED
    db.commit()
    db.refresh(task)
    return task


def mark_task_failed(db: Session, task_id: int) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = TASK_STATUS_FAILED
    db.commit()
    db.refresh(task)
    return task