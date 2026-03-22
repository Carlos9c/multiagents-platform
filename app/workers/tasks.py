from app.db.session import SessionLocal
from app.models.execution_run import (
    FAILURE_TYPE_INTERNAL,
    RECOVERY_ACTION_MANUAL_REVIEW,
    RECOVERY_ACTION_REATOMIZE,
)
from app.models.task import Task
from app.services.execution_runs import (
    get_execution_run,
    mark_execution_run_failed,
    mark_execution_run_rejected,
    mark_execution_run_started,
    mark_execution_run_succeeded,
)
from app.services.executor import (
    ExecutorInternalError,
    ExecutorRejectedError,
    execute_atomic_task,
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
    task: Task | None = None

    try:
        run = get_execution_run(db, run_id)
        if not run:
            raise ValueError(f"ExecutionRun {run_id} not found")

        task = db.get(Task, run.task_id)
        if not task:
            raise ValueError(f"Task {run.task_id} not found")

        mark_execution_run_started(db, run_id)
        mark_task_running(db, task.id)

        result = execute_atomic_task(db=db, task=task)

        mark_execution_run_succeeded(
            db=db,
            run_id=run_id,
            output_snapshot=result.output_snapshot,
        )
        mark_task_completed(db, task.id)

        return result.output_snapshot

    except ExecutorRejectedError as exc:
        run = get_execution_run(db, run_id)
        if run:
            mark_execution_run_rejected(
                db=db,
                run_id=run_id,
                error_message=exc.message,
                failure_code=exc.failure_code,
                recovery_action=RECOVERY_ACTION_REATOMIZE,
            )

            if task is None:
                task = db.get(Task, run.task_id)
            if task:
                mark_task_failed(db, task.id)

        return exc.failure_code

    except ExecutorInternalError as exc:
        run = get_execution_run(db, run_id)
        if run:
            mark_execution_run_failed(
                db=db,
                run_id=run_id,
                error_message=exc.message,
                failure_type=FAILURE_TYPE_INTERNAL,
                failure_code=exc.failure_code,
                recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
            )

            if task is None:
                task = db.get(Task, run.task_id)
            if task:
                mark_task_failed(db, task.id)

        raise

    except Exception as exc:
        run = get_execution_run(db, run_id)
        if run:
            mark_execution_run_failed(
                db=db,
                run_id=run_id,
                error_message=str(exc),
                failure_type=FAILURE_TYPE_INTERNAL,
                failure_code="worker_execution_error",
                recovery_action=RECOVERY_ACTION_MANUAL_REVIEW,
            )

            if task is None:
                task = db.get(Task, run.task_id)
            if task:
                mark_task_failed(db, task.id)

        raise

    finally:
        db.close()