from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.project import Project
from app.models.task import Task
from app.schemas.task import TaskCreate, TaskRead
from app.services.execution_runs import create_execution_run
from app.workers.tasks import execute_task as execute_task_job

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskRead)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)):
    project = db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if payload.parent_task_id is not None:
        parent_task = db.get(Task, payload.parent_task_id)
        if not parent_task:
            raise HTTPException(status_code=404, detail="Parent task not found")
        if parent_task.project_id != payload.project_id:
            raise HTTPException(
                status_code=400,
                detail="Parent task must belong to the same project",
            )

    task = Task(
        project_id=payload.project_id,
        parent_task_id=payload.parent_task_id,
        title=payload.title,
        description=payload.description,
        summary=payload.summary,
        objective=payload.objective,
        proposed_solution=payload.proposed_solution,
        implementation_notes=payload.implementation_notes,
        implementation_steps=payload.implementation_steps,
        acceptance_criteria=payload.acceptance_criteria,
        tests_required=payload.tests_required,
        technical_constraints=payload.technical_constraints,
        out_of_scope=payload.out_of_scope,
        priority=payload.priority,
        task_type=payload.task_type,
        planning_level=payload.planning_level,
        executor_type=payload.executor_type,
        sequence_order=payload.sequence_order,
        status=payload.status,
        is_blocked=payload.is_blocked,
        blocking_reason=payload.blocking_reason,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@router.get("", response_model=list[TaskRead])
def list_tasks(db: Session = Depends(get_db)):
    return db.query(Task).order_by(Task.id.asc()).all()


@router.post("/{task_id}/execute")
def execute_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.is_blocked:
        raise HTTPException(
            status_code=400,
            detail=f"Task is blocked: {task.blocking_reason or 'no reason provided'}",
        )

    if task.executor_type != "code_executor":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported executor_type for current executor: {task.executor_type}",
        )

    if task.planning_level not in {"refined", "atomic"}:
        raise HTTPException(
            status_code=400,
            detail="Only refined or atomic tasks can be executed",
        )

    run = create_execution_run(
        db=db,
        task_id=task.id,
        agent_name="executor_agent",
        input_snapshot=f"Executing task {task.id}: {task.title}",
    )

    async_result = execute_task_job.delay(run.id)

    return {
        "execution_run_id": run.id,
        "celery_task_id": async_result.id,
        "status": run.status,
    }