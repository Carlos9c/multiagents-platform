from app.db.session import SessionLocal
from app.models.execution_run import ExecutionRun
from app.models.task import Task
from app.services.artifacts import create_artifact
from app.services.execution_runs import (
    mark_execution_run_failed,
    mark_execution_run_started,
    mark_execution_run_succeeded,
)
from app.services.tasks import (
    mark_task_completed,
    mark_task_failed,
    mark_task_running,
)
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.tasks.execute_task")
def execute_task(run_id: int) -> str:
    db = SessionLocal()
    try:
        run = db.get(ExecutionRun, run_id)
        if not run:
            raise ValueError(f"ExecutionRun {run_id} not found")

        task = db.get(Task, run.task_id)
        if not task:
            raise ValueError(f"Task {run.task_id} not found")

        mark_execution_run_started(db, run_id)
        mark_task_running(db, task.id)

        implementation_brief = f"""
Task ID: {task.id}
Title: {task.title}
Type: {task.task_type}
Description: {task.description or "No description provided"}

Objective:
Produce the implementation brief for this task.

Definition of Done:
- Understand task scope
- Prepare a concise implementation brief
- Store it as an artifact
""".strip()

        create_artifact(
            db=db,
            project_id=task.project_id,
            task_id=task.id,
            artifact_type="implementation_brief",
            content=implementation_brief,
            created_by="executor_agent",
        )

        mark_execution_run_succeeded(
            db,
            run_id,
            output_snapshot="implementation_brief_created",
        )
        mark_task_completed(db, task.id)

        return "implementation_brief_created"

    except Exception as exc:
        run = db.get(ExecutionRun, run_id)
        if run:
            mark_execution_run_failed(db, run_id, str(exc))
            mark_task_failed(db, run.task_id)
        raise
    finally:
        db.close()