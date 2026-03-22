from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.task import (
    CODE_EXECUTOR,
    EXECUTABLE_TASK_STATUSES,
    PLANNING_LEVEL_ATOMIC,
    PENDING_ATOMIC_ASSIGNMENT_EXECUTOR,
    Task,
)
from app.services.execution_runs import create_execution_run
from app.workers.tasks import execute_task as execute_task_job

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
)

SUPPORTED_EXECUTORS = {
    CODE_EXECUTOR,
}


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

    if task.planning_level != PLANNING_LEVEL_ATOMIC:
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

    if task.executor_type not in SUPPORTED_EXECUTORS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported executor_type '{task.executor_type}'. "
                f"Supported executors: {sorted(SUPPORTED_EXECUTORS)}"
            ),
        )

    if task.status not in EXECUTABLE_TASK_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Task status '{task.status}' is not executable. "
                f"Allowed statuses: {sorted(EXECUTABLE_TASK_STATUSES)}"
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