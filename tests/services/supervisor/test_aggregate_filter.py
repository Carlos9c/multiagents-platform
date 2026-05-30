"""
Tests for aggregate_filter.py — report selection logic.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.agent_evaluation import AgentEvaluation
from app.models.supervisor_report import (
    SUPERVISOR_REPORT_STATUS_COMPLETED,
    SUPERVISOR_REPORT_STATUS_FAILED,
    SupervisorReport,
)
from app.services.supervisor.aggregate_filter import (
    TooFewProjectsError,
    _is_clean_version,
    _report_is_dirty,
    select_reports,
)

_MODULE = "app.services.supervisor.aggregate_filter"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(
    db: Session,
    *,
    project_id: int,
    status: str = SUPERVISOR_REPORT_STATUS_COMPLETED,
    overall_verdict: str = "healthy",
    created_at: datetime | None = None,
    system_versions: list[str] | None = None,
) -> SupervisorReport:
    report = SupervisorReport(
        project_id=project_id,
        status=status,
        overall_verdict=overall_verdict,
        created_at=created_at or datetime.now(timezone.utc),
        synthesis="Some synthesis text.",
        analyzed_run_ids=[],
    )
    db.add(report)
    db.flush()

    versions = system_versions if system_versions is not None else ["abc1234"]
    ev = AgentEvaluation(
        report_id=report.report_id,
        agent_name="planner",
        project_id=project_id,
        verdict="healthy",
        findings="Good.",
        system_versions_seen=versions,
    )
    db.add(ev)
    db.commit()
    db.refresh(report)
    return report


def _make_five_reports(db: Session, make_project) -> list[SupervisorReport]:
    reports = []
    for i in range(5):
        proj = make_project(name=f"Project {i}")
        r = _make_report(db, project_id=proj.id, system_versions=["abc1234"])
        reports.append(r)
    return reports


# ---------------------------------------------------------------------------
# _is_clean_version
# ---------------------------------------------------------------------------


def test_is_clean_version_accepts_7_char_hash():
    assert _is_clean_version("abc1234") is True


def test_is_clean_version_accepts_40_char_hash():
    assert _is_clean_version("a" * 40) is True


def test_is_clean_version_rejects_dirty_suffix():
    assert _is_clean_version("abc1234-dirty") is False


def test_is_clean_version_rejects_empty():
    assert _is_clean_version("") is False


def test_is_clean_version_rejects_non_hex():
    assert _is_clean_version("xyz1234") is False


# ---------------------------------------------------------------------------
# _report_is_dirty
# ---------------------------------------------------------------------------


def test_report_is_dirty_when_no_versions():
    ev = AgentEvaluation(report_id=1, agent_name="planner", project_id=1, system_versions_seen=[])
    assert _report_is_dirty([ev]) is True


def test_report_is_dirty_when_versions_none():
    ev = AgentEvaluation(report_id=1, agent_name="planner", project_id=1, system_versions_seen=None)
    assert _report_is_dirty([ev]) is True


def test_report_is_clean_when_one_clean_version():
    ev = AgentEvaluation(
        report_id=1,
        agent_name="planner",
        project_id=1,
        system_versions_seen=["abc1234"],
    )
    assert _report_is_dirty([ev]) is False


def test_report_is_clean_even_if_one_dirty_among_clean():
    ev = AgentEvaluation(
        report_id=1,
        agent_name="planner",
        project_id=1,
        system_versions_seen=["abc1234-dirty", "def5678"],
    )
    assert _report_is_dirty([ev]) is False


# ---------------------------------------------------------------------------
# select_reports — basic selection
# ---------------------------------------------------------------------------


def test_select_reports_requires_minimum_five(db_session: Session, make_project):
    # Only 4 projects — should raise
    for i in range(4):
        proj = make_project(name=f"Too few {i}")
        _make_report(db_session, project_id=proj.id)

    with pytest.raises(TooFewProjectsError, match="5 projects"):
        select_reports(db_session, limit=20)


def test_select_reports_happy_path(db_session: Session, make_project):
    _make_five_reports(db_session, make_project)
    reports, dirty = select_reports(db_session, limit=20)
    assert len(reports) == 5
    assert dirty == 0


def test_select_reports_excludes_failed_reports(db_session: Session, make_project):
    # 5 completed + 2 failed → only 5 returned
    for i in range(5):
        proj = make_project(name=f"Completed {i}")
        _make_report(db_session, project_id=proj.id)
    for i in range(2):
        proj = make_project(name=f"Failed {i}")
        _make_report(
            db_session,
            project_id=proj.id,
            status=SUPERVISOR_REPORT_STATUS_FAILED,
        )

    reports, _ = select_reports(db_session, limit=20)
    assert len(reports) == 5
    assert all(r.status == SUPERVISOR_REPORT_STATUS_COMPLETED for r in reports)


def test_select_reports_one_per_project(db_session: Session, make_project):
    proj = make_project(name="Multi-report project")
    # Create 3 reports for the same project — only the most recent should be kept
    for _ in range(3):
        _make_report(db_session, project_id=proj.id)
    for i in range(4):
        other = make_project(name=f"Other {i}")
        _make_report(db_session, project_id=other.id)

    reports, _ = select_reports(db_session, limit=20)
    project_ids = [r.project_id for r in reports]
    assert len(project_ids) == len(set(project_ids)), "Duplicate project in result"


def test_select_reports_caps_at_20(db_session: Session, make_project):
    for i in range(25):
        proj = make_project(name=f"Project {i}")
        _make_report(db_session, project_id=proj.id)

    reports, _ = select_reports(db_session, limit=20)
    assert len(reports) == 20


def test_select_reports_limit_parameter(db_session: Session, make_project):
    for i in range(10):
        proj = make_project(name=f"Project {i}")
        _make_report(db_session, project_id=proj.id)

    reports, _ = select_reports(db_session, limit=7)
    assert len(reports) == 7


# ---------------------------------------------------------------------------
# select_reports — version filter
# ---------------------------------------------------------------------------


def test_version_filter_matches_exact_version(db_session: Session, make_project):
    # 5 with target version, 2 without
    for i in range(5):
        proj = make_project(name=f"Match {i}")
        _make_report(db_session, project_id=proj.id, system_versions=["deadbeef"])
    for i in range(2):
        proj = make_project(name=f"No match {i}")
        _make_report(db_session, project_id=proj.id, system_versions=["aaaabbbb"])

    reports, _ = select_reports(db_session, version="deadbeef")
    assert len(reports) == 5
    assert all(
        any("deadbeef" in (ev.system_versions_seen or []) for ev in r.agent_evaluations)
        for r in reports
    )


def test_version_filter_only_matches_exact_version(db_session: Session, make_project):
    # Dirty reports (no clean hash) can't match a version filter — they're excluded
    # by the version check itself, not by dirty detection.
    for i in range(5):
        proj = make_project(name=f"Match {i}")
        _make_report(db_session, project_id=proj.id, system_versions=["deadbeef"])
    for i in range(3):
        proj = make_project(name=f"No-version {i}")
        _make_report(db_session, project_id=proj.id, system_versions=[])

    # The no-version ones don't match "deadbeef" → excluded by version filter (dirty_excluded=0)
    reports, dirty = select_reports(db_session, version="deadbeef")
    assert len(reports) == 5
    assert dirty == 0  # excluded by version filter, not dirty filter


def test_date_filter_excludes_dirty_when_forced(db_session: Session, make_project):
    dt = datetime(2026, 5, 1, tzinfo=timezone.utc)
    # 5 dirty (no clean hash) + 5 clean
    for i in range(5):
        proj = make_project(name=f"Dirty {i}")
        _make_report(
            db_session,
            project_id=proj.id,
            created_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
            system_versions=[],  # dirty — no clean hash
        )
    for i in range(5):
        proj = make_project(name=f"Clean {i}")
        _make_report(
            db_session,
            project_id=proj.id,
            created_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
            system_versions=["abc1234"],
        )

    # Explicitly exclude dirty
    reports, dirty = select_reports(db_session, date_from=dt, include_dirty=False)
    assert len(reports) == 5
    assert dirty == 5


# ---------------------------------------------------------------------------
# select_reports — date filter
# ---------------------------------------------------------------------------


def test_date_filter_includes_dirty_by_default(db_session: Session, make_project):
    dt = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i in range(5):
        proj = make_project(name=f"Dirty {i}")
        _make_report(
            db_session,
            project_id=proj.id,
            created_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
            system_versions=[],  # dirty — no clean hash
        )

    # Date-only filter → include_dirty defaults to True → all 5 included
    reports, dirty = select_reports(db_session, date_from=dt)
    assert len(reports) == 5
    assert dirty == 0  # dirty included, so nothing was excluded


def test_date_filter_from_excludes_older(db_session: Session, make_project):
    dt_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    dt_new = datetime(2026, 5, 1, tzinfo=timezone.utc)
    cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)

    for i in range(3):
        proj = make_project(name=f"Old {i}")
        _make_report(db_session, project_id=proj.id, created_at=dt_old)
    for i in range(5):
        proj = make_project(name=f"New {i}")
        _make_report(db_session, project_id=proj.id, created_at=dt_new)

    reports, _ = select_reports(db_session, date_from=cutoff)
    assert len(reports) == 5
    cutoff_naive = cutoff.replace(tzinfo=None)
    assert all(
        (r.created_at.replace(tzinfo=None) if r.created_at else r.created_at) >= cutoff_naive
        for r in reports
    )


# ---------------------------------------------------------------------------
# select_reports — project_ids filter
# ---------------------------------------------------------------------------


def test_project_ids_filter(db_session: Session, make_project):
    report_list = _make_five_reports(db_session, make_project)
    # Also add 3 more that should be excluded
    for i in range(3):
        proj = make_project(name=f"Extra {i}")
        _make_report(db_session, project_id=proj.id)

    target_ids = {r.project_id for r in report_list}
    reports, _ = select_reports(db_session, project_ids=list(target_ids))
    assert {r.project_id for r in reports} == target_ids
