"""
Aggregate supervisor runner.

Orchestrates: filter → build frequency table + corpus → LLM synthesis → persist.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.aggregate_report import (
    AGGREGATE_REPORT_STATUS_COMPLETED,
    AGGREGATE_REPORT_STATUS_FAILED,
    AggregateReport,
)
from app.services.supervisor.aggregate_builder import build_frequency_table, build_text_corpus
from app.services.supervisor.aggregate_filter import select_reports
from app.services.supervisor.aggregate_synthesizer import synthesize

logger = logging.getLogger(__name__)


def _build_filter_description(
    *,
    version: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    project_ids: list[int] | None,
    limit: int | None,
    include_dirty: bool | None,
) -> str:
    parts: list[str] = []
    if version:
        parts.append(f"version={version}")
    if date_from:
        parts.append(f"from={date_from.date()}")
    if date_to:
        parts.append(f"to={date_to.date()}")
    if project_ids:
        parts.append(f"projects={sorted(project_ids)}")
    if limit:
        parts.append(f"limit={limit}")
    if include_dirty is not None:
        parts.append(f"include_dirty={include_dirty}")
    return ", ".join(parts) if parts else "no filters"


def run_aggregate(
    db: Session,
    *,
    version: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    project_ids: list[int] | None = None,
    limit: int | None = None,
    include_dirty: bool | None = None,
) -> AggregateReport:
    """Run the full aggregate pipeline and return the persisted AggregateReport."""
    filter_params = {
        "version": version,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "project_ids": project_ids,
        "limit": limit,
        "include_dirty": include_dirty,
    }

    # Will raise TooFewProjectsError if < 5 projects match
    reports, dirty_excluded = select_reports(
        db,
        version=version,
        date_from=date_from,
        date_to=date_to,
        project_ids=project_ids,
        limit=limit,
        include_dirty=include_dirty,
    )

    supervisor_report_ids = [r.report_id for r in reports]
    analyzed_project_ids = [r.project_id for r in reports]
    project_count = len(reports)

    frequency_table = build_frequency_table(reports)
    text_corpus = build_text_corpus(reports)

    filter_description = _build_filter_description(
        version=version,
        date_from=date_from,
        date_to=date_to,
        project_ids=project_ids,
        limit=limit,
        include_dirty=include_dirty,
    )

    try:
        synthesis_text = synthesize(
            project_count=project_count,
            filter_description=filter_description,
            frequency_table=frequency_table,
            text_corpus=text_corpus,
        )
        status = AGGREGATE_REPORT_STATUS_COMPLETED
        error_message = None
    except Exception as exc:
        logger.error("aggregate_synthesis_failed error=%s", exc, exc_info=True)
        synthesis_text = f"Synthesis generation failed: {exc}"
        status = AGGREGATE_REPORT_STATUS_FAILED
        error_message = str(exc)

    report = AggregateReport(
        filter_params=filter_params,
        supervisor_report_ids=supervisor_report_ids,
        project_ids_analyzed=analyzed_project_ids,
        dirty_projects_excluded=dirty_excluded,
        agent_frequency_table=frequency_table,
        synthesis=synthesis_text,
        status=status,
        error_message=error_message,
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    logger.info(
        "aggregate_completed report_id=%s projects=%s dirty_excluded=%s status=%s",
        report.aggregate_report_id,
        project_count,
        dirty_excluded,
        status,
    )
    return report
