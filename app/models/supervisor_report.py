from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

SUPERVISOR_REPORT_STATUS_PENDING = "pending"
SUPERVISOR_REPORT_STATUS_RUNNING = "running"
SUPERVISOR_REPORT_STATUS_COMPLETED = "completed"
SUPERVISOR_REPORT_STATUS_FAILED = "failed"

VALID_SUPERVISOR_REPORT_STATUSES = {
    SUPERVISOR_REPORT_STATUS_PENDING,
    SUPERVISOR_REPORT_STATUS_RUNNING,
    SUPERVISOR_REPORT_STATUS_COMPLETED,
    SUPERVISOR_REPORT_STATUS_FAILED,
}

# Overall verdict values stored in SupervisorReport.overall_verdict
SUPERVISOR_REPORT_VERDICT_HEALTHY = "healthy"
SUPERVISOR_REPORT_VERDICT_NEEDS_ATTENTION = "needs_attention"
SUPERVISOR_REPORT_VERDICT_DEGRADED = "degraded"
# Returned when >30% of agents could not be evaluated (not_supervised)
SUPERVISOR_REPORT_VERDICT_NOT_EVALUATED = "not_evaluated"


class SupervisorReport(Base):
    """
    One report per project per supervision run.

    A report aggregates the findings of all agent evaluations produced for a
    project at a given point in time.  The cross-agent synthesis and overall
    project-level verdict live here; per-agent detail lives in AgentEvaluation.
    """

    __tablename__ = "supervisor_reports"

    report_id: Mapped[int] = mapped_column(primary_key=True, index=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=SUPERVISOR_REPORT_STATUS_PENDING,
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSON-encoded list[int] of ExecutionRun IDs that were covered by this report
    analyzed_run_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Cross-agent synthesis text produced by the supervisor
    synthesis: Mapped[str | None] = mapped_column(Text, nullable=True)

    # High-level verdict: "healthy" | "needs_attention" | "degraded" | "not_evaluated"
    overall_verdict: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=func.now(),
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    project = relationship("Project", backref="supervisor_reports")
    agent_evaluations = relationship(
        "AgentEvaluation",
        back_populates="report",
        cascade="all, delete-orphan",
    )
