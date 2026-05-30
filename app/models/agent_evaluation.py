from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

AGENT_EVALUATION_VERDICT_HEALTHY = "healthy"
AGENT_EVALUATION_VERDICT_NEEDS_ATTENTION = "needs_attention"
AGENT_EVALUATION_VERDICT_DEGRADED = "degraded"

VALID_AGENT_EVALUATION_VERDICTS = {
    AGENT_EVALUATION_VERDICT_HEALTHY,
    AGENT_EVALUATION_VERDICT_NEEDS_ATTENTION,
    AGENT_EVALUATION_VERDICT_DEGRADED,
}


class AgentEvaluation(Base):
    """
    One row per (supervisor_report, agent_name).

    Composite primary key — no surrogate id column.  Stores the aggregated
    findings the supervisor produced for a single agent across the whole
    project at the time of the report.
    """

    __tablename__ = "agent_evaluations"

    report_id: Mapped[int] = mapped_column(
        ForeignKey("supervisor_reports.report_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )

    agent_name: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
        nullable=False,
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )

    # JSON-encoded list[int] — which execution run IDs were consulted (empty for
    # planning-layer agents that are not tied to a single run)
    execution_run_ids_analyzed: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # JSON-encoded list[str] — git commit hashes seen across analyzed runs
    system_versions_seen: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # One of healthy / needs_attention / degraded
    verdict: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Prose synthesis specific to this agent
    findings: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSON-encoded list[str] — concrete issues identified
    issues: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # JSON-encoded list[str] — actionable improvement suggestions
    suggestions: Mapped[list | None] = mapped_column(JSON, nullable=True)

    report = relationship("SupervisorReport", back_populates="agent_evaluations")
    project = relationship("Project", backref="agent_evaluations")
