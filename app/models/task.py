from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

PLANNING_LEVEL_HIGH_LEVEL = "high_level"
PLANNING_LEVEL_REFINED = "refined"
PLANNING_LEVEL_ATOMIC = "atomic"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_AWAITING_VALIDATION = "awaiting_validation"
TASK_STATUS_AWAITING_REVIEW = "awaiting_review"
TASK_STATUS_PARTIAL = "partial"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_REATOMIZED = "reatomized"
TASK_STATUS_FOLLOWED_UP = "followed_up"
TASK_STATUS_SUPERSEDED = "superseded"

TERMINAL_TASK_STATUSES = {
    TASK_STATUS_PARTIAL,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_REATOMIZED,
    TASK_STATUS_FOLLOWED_UP,
    TASK_STATUS_SUPERSEDED,
}

# executor_type sigue existiendo como contrato de dominio de alto nivel.
# Valores canónicos:
# - pending_engine_routing: la task todavía no tiene routing final resuelto
# - execution_engine: la task será ejecutada por el engine orquestado
PENDING_ENGINE_ROUTING_EXECUTOR = "pending_engine_routing"
EXECUTION_ENGINE = "execution_engine"
DOCUMENTATION_ENGINE = "documentation_engine"

VERIFICATION_LEVEL_NONE = "none"
VERIFICATION_LEVEL_RUNTIME = "runtime"

VALID_PLANNING_LEVELS = {
    PLANNING_LEVEL_HIGH_LEVEL,
    PLANNING_LEVEL_REFINED,
    PLANNING_LEVEL_ATOMIC,
}

VALID_TASK_STATUSES = {
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    TASK_STATUS_AWAITING_VALIDATION,
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_REATOMIZED,
    TASK_STATUS_FOLLOWED_UP,
    TASK_STATUS_SUPERSEDED,
}

VALID_EXECUTOR_TYPES = {
    PENDING_ENGINE_ROUTING_EXECUTOR,
    EXECUTION_ENGINE,
    DOCUMENTATION_ENGINE,
}

VALID_TASK_TYPES = frozenset(
    {
        "requirements",
        "design",
        "planning",
        "implementation",
        "testing",
        "review",
        "documentation",
        "onboarding",
        "configuration",
        "refactor",
    }
)

EXECUTABLE_TASK_STATUSES = {
    TASK_STATUS_PENDING,
    TASK_STATUS_FAILED,
}


def is_valid_executor_type(executor_type: str | None) -> bool:
    return executor_type in VALID_EXECUTOR_TYPES


def is_valid_task_type(task_type: str | None) -> bool:
    return task_type in VALID_TASK_TYPES


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )

    parent_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id"),
        nullable=True,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_solution: Mapped[str | None] = mapped_column(Text, nullable=True)
    implementation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    implementation_steps: Mapped[str | None] = mapped_column(Text, nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    tests_required: Mapped[str | None] = mapped_column(Text, nullable=True)
    technical_constraints: Mapped[str | None] = mapped_column(Text, nullable=True)
    out_of_scope: Mapped[str | None] = mapped_column(Text, nullable=True)

    priority: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="medium",
    )

    task_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="implementation",
    )

    planning_level: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=PLANNING_LEVEL_HIGH_LEVEL,
    )

    executor_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=PENDING_ENGINE_ROUTING_EXECUTOR,
    )

    verification_level: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=VERIFICATION_LEVEL_RUNTIME,
    )

    last_execution_agent_sequence: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    sequence_order: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=TASK_STATUS_PENDING,
    )

    is_blocked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    blocking_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_recovery_task: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    followup_depth: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    revised_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    review_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    project = relationship("Project", backref="tasks")
    parent_task = relationship("Task", remote_side=[id], backref="child_tasks")
