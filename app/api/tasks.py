from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.services.task_execution_service import (
    TaskExecutionServiceError,
    execute_task_sync,
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
        result = execute_task_sync(db=db, task_id=task_id)
        return {
            "message": result.message,
            "task_id": result.task_id,
            "execution_run_id": result.execution_run_id,
            "run_status": result.run_status,
            "executor_type": result.executor_type,
            "output_snapshot": result.output_snapshot,
            "final_task_status": result.final_task_status,
            "validation_decision": result.validation_decision,
        }
    except TaskExecutionServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc