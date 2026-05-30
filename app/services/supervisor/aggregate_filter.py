"""
Report selection logic for the aggregate supervisor.

Filters SupervisorReport records by version, date range, explicit project list,
or recency limit. Handles dirty-project exclusion.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.agent_evaluation import AgentEvaluation
from app.models.supervisor_report import SUPERVISOR_REPORT_STATUS_COMPLETED, SupervisorReport

logger = logging.getLogger(__name__)

_MIN_PROJECTS = 5


_MAX_PROJECTS = 20

# A clean git hash: 7–40 lowercase hex chars (no suffix, no spaces)
_CLEAN_HASH_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _as_naive_utc(dt: datetime) -> datetime:
    """Normalize to naive UTC for SQLite-safe comparison."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _is_clean_version(version: str) -> bool:
    return bool(_CLEAN_HASH_RE.match(version))


def _report_is_dirty(evaluations: list[AgentEvaluation]) -> bool:
    """A report is dirty when none of its system_versions_seen contains a clean git hash."""
    for ev in evaluations:
        versions: list = ev.system_versions_seen or []
        if isinstance(versions, list):
            for v in versions:
                if v and _is_clean_version(str(v)):
                    return False
    return True


def _report_contains_version(evaluations: list[AgentEvaluation], version: str) -> bool:
    """True if the target version appears in any AgentEvaluation.system_versions_seen."""
    for ev in evaluations:
        versions: list = ev.system_versions_seen or []
        if isinstance(versions, list) and version in versions:
            return True
    return False


class AggregateFilterError(Exception):
    pass


class TooFewProjectsError(AggregateFilterError):
    pass


def select_reports(
    db: Session,
    *,
    version: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    project_ids: list[int] | None = None,
    limit: int | None = None,
    include_dirty: bool | None = None,
) -> tuple[list[SupervisorReport], int]:
    """Select SupervisorReports for aggregation.

    Returns (selected_reports, dirty_excluded_count).

    Rules:
    - Only completed reports are eligible; one per project (most recent).
    - include_dirty defaults to False for version filters, True for date-only filters.
    - Results are capped at _MAX_PROJECTS (20) after all filters.
    - Raises TooFewProjectsError if fewer than _MIN_PROJECTS remain after filtering.
    """
    # --- load all completed reports, most recent per project ---
    all_reports: list[SupervisorReport] = (
        db.query(SupervisorReport)
        .filter(SupervisorReport.status == SUPERVISOR_REPORT_STATUS_COMPLETED)
        .order_by(SupervisorReport.created_at.desc())
        .all()
    )

    # Keep only the most recent report per project
    seen_projects: set[int] = set()
    latest_per_project: list[SupervisorReport] = []
    for r in all_reports:
        if r.project_id not in seen_projects:
            seen_projects.add(r.project_id)
            latest_per_project.append(r)

    # Eager-load evaluations per report (needed for dirty check and version check)
    report_evals: dict[int, list[AgentEvaluation]] = {}
    for r in latest_per_project:
        report_evals[r.report_id] = list(r.agent_evaluations)

    # --- apply explicit project_ids filter ---
    if project_ids is not None:
        project_id_set = set(project_ids)
        latest_per_project = [r for r in latest_per_project if r.project_id in project_id_set]

    # --- apply date range filter ---
    if date_from is not None:
        df_naive = _as_naive_utc(date_from)
        latest_per_project = [
            r
            for r in latest_per_project
            if r.created_at and _as_naive_utc(r.created_at) >= df_naive
        ]
    if date_to is not None:
        dt_naive = _as_naive_utc(date_to)
        latest_per_project = [
            r
            for r in latest_per_project
            if r.created_at and _as_naive_utc(r.created_at) <= dt_naive
        ]

    # --- apply version filter ---
    if version is not None:
        latest_per_project = [
            r
            for r in latest_per_project
            if _report_contains_version(report_evals[r.report_id], version)
        ]

    # --- determine effective include_dirty ---
    # Default: True when filtering by date (temporal placement is always valid),
    # False when filtering by version (dirty projects can't be reliably placed).
    if include_dirty is None:
        effective_include_dirty = version is None
    else:
        effective_include_dirty = include_dirty

    # --- dirty filter ---
    dirty_excluded = 0
    if not effective_include_dirty:
        clean = []
        for r in latest_per_project:
            if _report_is_dirty(report_evals[r.report_id]):
                dirty_excluded += 1
                logger.debug("aggregate_filter: excluding dirty report_id=%s", r.report_id)
            else:
                clean.append(r)
        latest_per_project = clean

    # --- apply recency limit then cap at MAX_PROJECTS ---
    cap = min(limit, _MAX_PROJECTS) if limit is not None else _MAX_PROJECTS
    latest_per_project = latest_per_project[:cap]

    if len(latest_per_project) < _MIN_PROJECTS:
        raise TooFewProjectsError(
            f"Aggregate analysis requires at least {_MIN_PROJECTS} projects, "
            f"but only {len(latest_per_project)} matched the filter."
        )

    return latest_per_project, dirty_excluded
