from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


EXECUTION_RUN_STATUS_PENDING = "pending"
EXECUTION_RUN_STATUS_RUNNING = "running"
EXECUTION_RUN_STATUS_SUCCEEDED = "succeeded"
EXECUTION_RUN_STATUS_FAILED = "failed"
EXECUTION_RUN_STATUS_REJECTED = "rejected"

VALID_EXECUTION_RUN_STATUSES = {
    EXECUTION_RUN_STATUS_PENDING,
    EXECUTION_RUN_STATUS_RUNNING,
    EXECUTION_RUN_STATUS_SUCCEEDED,
    EXECUTION_RUN_STATUS_FAILED,
    EXECUTION_RUN_STATUS_REJECTED,
}

FAILURE_TYPE_TRANSIENT = "transient"
FAILURE_TYPE_VALIDATION = "validation"
FAILURE_TYPE_EXECUTOR_REJECTED = "executor_rejected"
FAILURE_TYPE_INTERNAL = "internal"
FAILURE_TYPE_UNKNOWN = "unknown"

VALID_FAILURE_TYPES = {
    FAILURE_TYPE_TRANSIENT,
    FAILURE_TYPE_VALIDATION,
    FAILURE_TYPE_EXECUTOR_REJECTED,
    FAILURE_TYPE_INTERNAL,
    FAILURE_TYPE_UNKNOWN,
}

RECOVERY_ACTION_NONE = "none"
RECOVERY_ACTION_RETRY_SAME_TASK = "retry_same_task"
RECOVERY_ACTION_REATOMIZE = "reatomize"
RECOVERY_ACTION_MANUAL_REVIEW = "manual_review"

VALID_RECOVERY_ACTIONS = {
    RECOVERY_ACTION_NONE,
    RECOVERY_ACTION_RETRY_SAME_TASK,
    RECOVERY_ACTION_REATOMIZE,
    RECOVERY_ACTION_MANUAL_REVIEW,
}


class ExecutionRun(Base):
    __tablename__ = "execution_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id"),
        nullable=False,
        index=True,
    )

    parent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_runs.id"),
        nullable=True,
        index=True,
    )

    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)

    attempt_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=EXECUTION_RUN_STATUS_PENDING,
    )

    input_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    failure_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recovery_action: Mapped[str | None] = mapped_column(String(50), nullable=True)

    task = relationship("Task", backref="execution_runs")
    parent_run = relationship(
        "ExecutionRun",
        remote_side=[id],
        backref="child_runs",
    )