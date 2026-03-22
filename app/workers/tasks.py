from app.db.session import SessionLocal
from app.services.task_execution_service import execute_existing_run_sync
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.tasks.execute_task")
def execute_task(run_id: int) -> str:
    db = SessionLocal()

    try:
        result = execute_existing_run_sync(db=db, run_id=run_id)
        return result.output_snapshot or result.run_status
    finally:
        db.close()