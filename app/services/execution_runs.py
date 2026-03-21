from sqlalchemy.orm import Session

from app.models.execution_run import ExecutionRun


def create_execution_run(
    db: Session,
    task_id: int,
    agent_name: str,
    input_snapshot: str | None = None,
) -> ExecutionRun:
    run = ExecutionRun(
        task_id=task_id,
        agent_name=agent_name,
        status="pending",
        input_snapshot=input_snapshot,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def mark_execution_run_started(db: Session, run_id: int) -> ExecutionRun | None:
    run = db.get(ExecutionRun, run_id)
    if not run:
        return None

    run.status = "running"
    db.commit()
    db.refresh(run)
    return run


def mark_execution_run_succeeded(
    db: Session,
    run_id: int,
    output_snapshot: str | None = None,
) -> ExecutionRun | None:
    run = db.get(ExecutionRun, run_id)
    if not run:
        return None

    run.status = "succeeded"
    run.output_snapshot = output_snapshot
    db.commit()
    db.refresh(run)
    return run


def mark_execution_run_failed(
    db: Session,
    run_id: int,
    error_message: str,
) -> ExecutionRun | None:
    run = db.get(ExecutionRun, run_id)
    if not run:
        return None

    run.status = "failed"
    run.error_message = error_message
    db.commit()
    db.refresh(run)
    return run