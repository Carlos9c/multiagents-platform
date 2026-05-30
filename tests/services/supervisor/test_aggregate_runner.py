"""
Tests for aggregate_runner.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models.agent_evaluation import AgentEvaluation
from app.models.aggregate_report import (
    AGGREGATE_REPORT_STATUS_COMPLETED,
    AGGREGATE_REPORT_STATUS_FAILED,
    AggregateReport,
)
from app.models.supervisor_report import (
    SUPERVISOR_REPORT_STATUS_COMPLETED,
    SupervisorReport,
)
from app.services.supervisor.aggregate_filter import TooFewProjectsError
from app.services.supervisor.aggregate_runner import run_aggregate

_MODULE = "app.services.supervisor.aggregate_runner"

_SYNTHESIS = (
    "## Critical Patterns\n\nOrchestrator degraded in 80% of projects. "
    "The budget is consistently exhausted before execution completes. "
    "Recommend increasing EXECUTION_ENGINE_MAX_STEPS. "
    "This is a systemic issue affecting all recent projects."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed_report(
    db: Session,
    *,
    project_id: int,
    system_versions: list[str] | None = None,
) -> SupervisorReport:
    report = SupervisorReport(
        project_id=project_id,
        status=SUPERVISOR_REPORT_STATUS_COMPLETED,
        overall_verdict="healthy",
        synthesis="Synthesis.",
        analyzed_run_ids=[],
        created_at=datetime.now(timezone.utc),
    )
    db.add(report)
    db.flush()
    ev = AgentEvaluation(
        report_id=report.report_id,
        agent_name="planner",
        project_id=project_id,
        verdict="healthy",
        findings="Good.",
        system_versions_seen=system_versions if system_versions is not None else ["abc1234"],
    )
    db.add(ev)
    db.commit()
    db.refresh(report)
    return report


def _make_five_reports(db: Session, make_project) -> list[SupervisorReport]:
    reports = []
    for i in range(5):
        proj = make_project(name=f"Project {i}")
        r = _make_completed_report(db, project_id=proj.id)
        reports.append(r)
    return reports


# ---------------------------------------------------------------------------
# TooFewProjectsError propagates
# ---------------------------------------------------------------------------


def test_run_aggregate_raises_when_too_few_projects(db_session: Session, make_project):
    for i in range(3):
        proj = make_project(name=f"P {i}")
        _make_completed_report(db_session, project_id=proj.id)

    with pytest.raises(TooFewProjectsError):
        run_aggregate(db_session, limit=20)


# ---------------------------------------------------------------------------
# Happy path — report persisted
# ---------------------------------------------------------------------------


def test_run_aggregate_persists_report(db_session: Session, make_project):
    _make_five_reports(db_session, make_project)

    with patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS):
        report = run_aggregate(db_session, limit=20)

    assert isinstance(report, AggregateReport)
    assert report.aggregate_report_id is not None
    assert report.status == AGGREGATE_REPORT_STATUS_COMPLETED
    assert report.synthesis == _SYNTHESIS
    assert len(report.project_ids_analyzed) == 5
    assert len(report.supervisor_report_ids) == 5


def test_run_aggregate_stores_frequency_table(db_session: Session, make_project):
    _make_five_reports(db_session, make_project)

    with patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS):
        report = run_aggregate(db_session, limit=20)

    assert isinstance(report.agent_frequency_table, list)
    assert len(report.agent_frequency_table) > 0
    first = report.agent_frequency_table[0]
    assert "agent_name" in first
    assert "degraded_pct" in first


def test_run_aggregate_stores_filter_params(db_session: Session, make_project):
    _make_five_reports(db_session, make_project)

    with patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS):
        report = run_aggregate(db_session, limit=10, version="abc1234")

    assert report.filter_params["limit"] == 10
    assert report.filter_params["version"] == "abc1234"


def test_run_aggregate_synthesis_failure_sets_failed_status(db_session: Session, make_project):
    _make_five_reports(db_session, make_project)

    with patch(f"{_MODULE}.synthesize", side_effect=RuntimeError("LLM error")):
        report = run_aggregate(db_session, limit=20)

    assert report.status == AGGREGATE_REPORT_STATUS_FAILED
    assert "LLM error" in report.error_message
    assert "LLM error" in report.synthesis


def test_run_aggregate_passes_project_count_to_synthesizer(db_session: Session, make_project):
    _make_five_reports(db_session, make_project)
    captured: dict = {}

    def capture_synthesize(**kwargs):
        captured.update(kwargs)
        return _SYNTHESIS

    with patch(f"{_MODULE}.synthesize", side_effect=capture_synthesize):
        run_aggregate(db_session, limit=20)

    assert captured["project_count"] == 5
    assert isinstance(captured["frequency_table"], list)
    assert isinstance(captured["text_corpus"], str)
    assert "limit=20" in captured["filter_description"]


def test_run_aggregate_dirty_excluded_count_stored(db_session: Session, make_project):
    # 5 clean + 2 dirty; use date_from + include_dirty=False to force dirty exclusion
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        proj = make_project(name=f"Clean {i}")
        _make_completed_report(db_session, project_id=proj.id, system_versions=["abc1234"])
    for i in range(2):
        proj = make_project(name=f"Dirty {i}")
        _make_completed_report(db_session, project_id=proj.id, system_versions=[])

    with patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS):
        report = run_aggregate(db_session, date_from=dt, include_dirty=False)

    assert report.dirty_projects_excluded == 2
    assert len(report.project_ids_analyzed) == 5


def test_run_aggregate_report_in_db_after_commit(db_session: Session, make_project):
    _make_five_reports(db_session, make_project)

    with patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS):
        report = run_aggregate(db_session, limit=20)

    stored = (
        db_session.query(AggregateReport)
        .filter(AggregateReport.aggregate_report_id == report.aggregate_report_id)
        .first()
    )
    assert stored is not None
    assert stored.synthesis == _SYNTHESIS


def test_run_aggregate_project_ids_filter(db_session: Session, make_project):
    all_reports = _make_five_reports(db_session, make_project)
    # Add 3 more that should be excluded
    for i in range(3):
        proj = make_project(name=f"Extra {i}")
        _make_completed_report(db_session, project_id=proj.id)

    target_ids = [r.project_id for r in all_reports]

    with patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS):
        report = run_aggregate(db_session, project_ids=target_ids)

    assert set(report.project_ids_analyzed) == set(target_ids)
