from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

AGGREGATE_REPORT_STATUS_COMPLETED = "completed"
AGGREGATE_REPORT_STATUS_FAILED = "failed"

VALID_AGGREGATE_REPORT_STATUSES = {
    AGGREGATE_REPORT_STATUS_COMPLETED,
    AGGREGATE_REPORT_STATUS_FAILED,
}


class AggregateReport(Base):
    """
    Cross-project aggregate supervisor report.

    Each row represents one invocation of POST /supervisor/aggregate — it captures
    which SupervisorReports were analyzed, the filter params used, the per-agent
    frequency table, and the LLM-produced narrative synthesis.

    No FK to SupervisorReport intentionally: if individual reports are later
    deleted, the aggregate snapshot remains valid as a historical record.
    """

    __tablename__ = "aggregate_reports"

    aggregate_report_id: Mapped[int] = mapped_column(primary_key=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # JSON-encoded dict of filter params used to build this report
    filter_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # JSON-encoded list[int] of SupervisorReport.report_id values included
    supervisor_report_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # JSON-encoded list[int] of project IDs analyzed
    project_ids_analyzed: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # How many dirty projects were excluded by the filter
    dirty_projects_excluded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # JSON-encoded list[dict] — per-agent verdict frequency table
    # Each entry: {agent_name, degraded_pct, needs_attention_pct, healthy_pct, not_supervised_pct, total_projects}
    agent_frequency_table: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # LLM-produced narrative Markdown ordered by severity
    synthesis: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default=AGGREGATE_REPORT_STATUS_COMPLETED
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
