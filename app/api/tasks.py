from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.task import Task
from app.services.execution_runs import create_execution_run
from app.workers.tasks import execute_task as execute_task_job

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
)


PENDING_ATOMIC_ASSIGNMENT_EXECUTOR = "pending_atomic_assignment"


@router.post("/{task_id}/execute")
def execute_task(
    task_id: int,
    db: Session = Depends(get_db),
):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if task.is_blocked:
        raise HTTPException(
            status_code=400,
            detail=f"Task is blocked: {task.blocking_reason or 'unknown reason'}",
        )

    if task.planning_level != "atomic":
        raise HTTPException(
            status_code=400,
            detail=(
                "Only atomic tasks can be executed. "
                "Executor assignment must be resolved during the atomic stage."
            ),
        )

    if not task.executor_type or task.executor_type == PENDING_ATOMIC_ASSIGNMENT_EXECUTOR:
        raise HTTPException(
            status_code=400,
            detail=(
                "Task executor is not assigned yet. "
                "Atomic task generation must assign a concrete executor before execution."
            ),
        )

    execution_run = create_execution_run(
        db=db,
        task_id=task.id,
        agent_name="executor_agent",
        input_snapshot=f"Executing task {task.id}: {task.title}",
    )

    async_result = execute_task_job.delay(execution_run.id)

    return {
        "message": "Execution started",
        "task_id": task.id,
        "execution_run_id": execution_run.id,
        "celery_task_id": async_result.id,
        "executor_type": task.executor_type,
    }