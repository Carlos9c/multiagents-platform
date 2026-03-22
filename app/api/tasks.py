from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.services.task_execution_service import (
    TaskExecutionServiceError,
    start_task_execution_async,
)

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
)


@router.post("/{task_id}/execute")
def execute_task(
    task_id: int,
    db: Session = Depends(get_db),
):
    try:
        result = start_task_execution_async(db=db, task_id=task_id)
        return {
            "message": result.message,
            "task_id": result.task_id,
            "execution_run_id": result.execution_run_id,
            "celery_task_id": result.celery_task_id,
            "executor_type": result.executor_type,
        }
    except TaskExecutionServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc