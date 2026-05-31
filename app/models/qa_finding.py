from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.qa_session import QASession

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

QA_SEVERITY_CRITICAL = "critical"
QA_SEVERITY_HIGH = "high"
QA_SEVERITY_MEDIUM = "medium"
QA_SEVERITY_LOW = "low"
QA_SEVERITY_INFO = "info"

QA_CATEGORY_FUNCTIONAL = "functional"
QA_CATEGORY_SECURITY = "security"
QA_CATEGORY_PERFORMANCE = "performance"
QA_CATEGORY_USABILITY = "usability"
QA_CATEGORY_ACCESSIBILITY = "accessibility"
QA_CATEGORY_COMPATIBILITY = "compatibility"
QA_CATEGORY_RELIABILITY = "reliability"
QA_CATEGORY_DATA_INTEGRITY = "data_integrity"
QA_CATEGORY_BOUNDARY = "boundary"
QA_CATEGORY_ADVERSARIAL = "adversarial"
QA_CATEGORY_REGRESSION = "regression"

VALID_QA_SEVERITIES = {
    QA_SEVERITY_CRITICAL,
    QA_SEVERITY_HIGH,
    QA_SEVERITY_MEDIUM,
    QA_SEVERITY_LOW,
    QA_SEVERITY_INFO,
}

VALID_QA_CATEGORIES = {
    QA_CATEGORY_FUNCTIONAL,
    QA_CATEGORY_SECURITY,
    QA_CATEGORY_PERFORMANCE,
    QA_CATEGORY_USABILITY,
    QA_CATEGORY_ACCESSIBILITY,
    QA_CATEGORY_COMPATIBILITY,
    QA_CATEGORY_RELIABILITY,
    QA_CATEGORY_DATA_INTEGRITY,
    QA_CATEGORY_BOUNDARY,
    QA_CATEGORY_ADVERSARIAL,
    QA_CATEGORY_REGRESSION,
}


class QAFinding(Base):
    __tablename__ = "qa_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    qa_session_id: Mapped[int] = mapped_column(
        ForeignKey("qa_sessions.id"), nullable=False, index=True
    )
    finding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reproduction_steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    affected_component: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auto_remediable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    remediation_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    remediation_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    # Which QA agent produced this finding (set since Phase 12 — null for legacy rows).
    producer_agent: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    session: Mapped["QASession"] = relationship("QASession", back_populates="findings")
