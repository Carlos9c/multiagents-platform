from sqlalchemy.orm import Session

from app.models.task import Task


def mark_task_running(db: Session, task_id: int) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = "running"
    db.commit()
    db.refresh(task)
    return task


def mark_task_completed(db: Session, task_id: int) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = "completed"
    db.commit()
    db.refresh(task)
    return task


def mark_task_failed(db: Session, task_id: int) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None

    task.status = "failed"
    db.commit()
    db.refresh(task)
    return task