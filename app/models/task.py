from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


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

    priority: Mapped[str] = mapped_column(String(50), nullable=False, default="medium")
    task_type: Mapped[str] = mapped_column(String(50), nullable=False, default="implementation")
    planning_level: Mapped[str] = mapped_column(String(50), nullable=False, default="high_level")
    executor_type: Mapped[str] = mapped_column(String(50), nullable=False, default="code_executor")

    sequence_order: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")

    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocking_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    project = relationship("Project", backref="tasks")
    parent_task = relationship("Task", remote_side=[id], backref="child_tasks")