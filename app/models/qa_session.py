from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.qa_finding import QAFinding

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

QA_SESSION_STATUS_PENDING = "pending"
QA_SESSION_STATUS_RUNNING = "running"
QA_SESSION_STATUS_COMPLETED = "completed"
QA_SESSION_STATUS_FAILED = "failed"
QA_SESSION_STATUS_BLOCKED = "blocked"

QA_VERDICT_PASSED = "passed"
QA_VERDICT_PARTIAL = "partial"
QA_VERDICT_FAILED = "failed"
QA_VERDICT_BLOCKED = "blocked"

VALID_QA_SESSION_STATUSES = {
    QA_SESSION_STATUS_PENDING,
    QA_SESSION_STATUS_RUNNING,
    QA_SESSION_STATUS_COMPLETED,
    QA_SESSION_STATUS_FAILED,
    QA_SESSION_STATUS_BLOCKED,
}

VALID_QA_VERDICTS = {
    QA_VERDICT_PASSED,
    QA_VERDICT_PARTIAL,
    QA_VERDICT_FAILED,
    QA_VERDICT_BLOCKED,
}


class QASession(Base):
    __tablename__ = "qa_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=QA_SESSION_STATUS_PENDING
    )
    product_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    strategy_used: Mapped[str | None] = mapped_column(String(50), nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(30), nullable=True)
    agents_called: Mapped[list | None] = mapped_column(JSON, nullable=True)
    findings_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Serialised list[ProbeRecord] — written by QAEngine after each run.
    # Each entry is a dict with at minimum: agent_name, probe_type, outcome, findings_count.
    probes: Mapped[list | None] = mapped_column(JSON, nullable=True)

    findings: Mapped[list["QAFinding"]] = relationship(
        "QAFinding",
        back_populates="session",
        order_by="QAFinding.id",
    )
