"""
Tests for the SupervisorReport and AgentEvaluation DB models.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.agent_evaluation import (
    AGENT_EVALUATION_VERDICT_DEGRADED,
    AGENT_EVALUATION_VERDICT_HEALTHY,
    AgentEvaluation,
)
from app.models.supervisor_report import (
    SUPERVISOR_REPORT_STATUS_COMPLETED,
    SUPERVISOR_REPORT_STATUS_PENDING,
    SupervisorReport,
)


def test_create_supervisor_report(db_session: Session, make_project):
    proj = make_project(name="Supervisor test project")

    report = SupervisorReport(
        project_id=proj.id,
        status=SUPERVISOR_REPORT_STATUS_PENDING,
    )
    db_session.add(report)
    db_session.commit()
    db_session.refresh(report)

    assert report.report_id is not None
    assert report.status == SUPERVISOR_REPORT_STATUS_PENDING
    assert report.project_id == proj.id


def test_supervisor_report_can_store_synthesis(db_session: Session, make_project):
    proj = make_project(name="Synthesis test")

    report = SupervisorReport(
        project_id=proj.id,
        status=SUPERVISOR_REPORT_STATUS_COMPLETED,
        synthesis="The project planning layer is generally healthy.",
        overall_verdict="healthy",
        analyzed_run_ids=[1, 2, 3],
    )
    db_session.add(report)
    db_session.commit()
    db_session.refresh(report)

    assert report.synthesis == "The project planning layer is generally healthy."
    assert report.overall_verdict == "healthy"
    assert report.analyzed_run_ids == [1, 2, 3]


def test_create_agent_evaluation_with_composite_pk(db_session: Session, make_project):
    proj = make_project(name="Agent eval test")

    report = SupervisorReport(
        project_id=proj.id,
        status=SUPERVISOR_REPORT_STATUS_COMPLETED,
    )
    db_session.add(report)
    db_session.flush()

    evaluation = AgentEvaluation(
        report_id=report.report_id,
        agent_name="planner",
        project_id=proj.id,
        verdict=AGENT_EVALUATION_VERDICT_HEALTHY,
        findings="Planner produced well-scoped tasks.",
        issues=[],
        suggestions=["Add more detail to implementation_notes."],
    )
    db_session.add(evaluation)
    db_session.commit()
    db_session.refresh(evaluation)

    assert evaluation.report_id == report.report_id
    assert evaluation.agent_name == "planner"
    assert evaluation.verdict == AGENT_EVALUATION_VERDICT_HEALTHY


def test_agent_evaluation_cascade_delete(db_session: Session, make_project):
    """Deleting a SupervisorReport should cascade-delete its AgentEvaluations."""
    proj = make_project(name="Cascade test")

    report = SupervisorReport(
        project_id=proj.id,
        status=SUPERVISOR_REPORT_STATUS_COMPLETED,
    )
    db_session.add(report)
    db_session.flush()

    eval1 = AgentEvaluation(
        report_id=report.report_id,
        agent_name="planner",
        project_id=proj.id,
        verdict=AGENT_EVALUATION_VERDICT_HEALTHY,
        findings="Healthy planner.",
    )
    eval2 = AgentEvaluation(
        report_id=report.report_id,
        agent_name="atomic_task_generator",
        project_id=proj.id,
        verdict=AGENT_EVALUATION_VERDICT_DEGRADED,
        findings="Issues with decomposition.",
        issues=["Too broad"],
    )
    db_session.add_all([eval1, eval2])
    db_session.commit()

    # Verify both evaluations exist
    count_before = (
        db_session.query(AgentEvaluation)
        .filter(AgentEvaluation.report_id == report.report_id)
        .count()
    )
    assert count_before == 2

    # Delete the report
    db_session.delete(report)
    db_session.commit()

    # Evaluations should be gone
    count_after = (
        db_session.query(AgentEvaluation)
        .filter(AgentEvaluation.report_id == report.report_id)
        .count()
    )
    assert count_after == 0


def test_multiple_agents_per_report(db_session: Session, make_project):
    proj = make_project(name="Multi-agent report")

    report = SupervisorReport(
        project_id=proj.id,
        status=SUPERVISOR_REPORT_STATUS_COMPLETED,
    )
    db_session.add(report)
    db_session.flush()

    agents = ["planner", "atomic_task_generator", "execution_sequencer"]
    for agent_name in agents:
        db_session.add(
            AgentEvaluation(
                report_id=report.report_id,
                agent_name=agent_name,
                project_id=proj.id,
                verdict=AGENT_EVALUATION_VERDICT_HEALTHY,
                findings=f"{agent_name} performed well.",
            )
        )
    db_session.commit()

    evaluations = (
        db_session.query(AgentEvaluation)
        .filter(AgentEvaluation.report_id == report.report_id)
        .all()
    )
    assert len(evaluations) == 3
    agent_names = {e.agent_name for e in evaluations}
    assert agent_names == set(agents)
