"""Tests for QASession and QAFinding models — Fase 1."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_QA_RUNNING,
    VALID_CONVERSATION_PHASES,
    Conversation,
)
from app.models.qa_finding import QAFinding
from app.models.qa_session import (
    QA_SESSION_STATUS_COMPLETED,
    QA_SESSION_STATUS_PENDING,
    QA_VERDICT_PASSED,
    QASession,
)

# ── Conversation new fields ────────────────────────────────────────────────────


def test_qa_running_phase_in_valid_phases() -> None:
    assert CONVERSATION_PHASE_QA_RUNNING in VALID_CONVERSATION_PHASES


def test_conversation_qa_offer_pending_defaults_false(
    db_session: Session, make_project: object
) -> None:
    project = make_project()
    conv = Conversation(
        project_id=project.id,
        status="active",
        phase="completed",
    )
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    assert conv.qa_offer_pending is False
    assert conv.pending_qa_report is None


def test_conversation_qa_fields_persist(db_session: Session, make_project: object) -> None:
    project = make_project()
    conv = Conversation(
        project_id=project.id,
        status="active",
        phase="completed",
        qa_offer_pending=True,
        pending_qa_report='{"verdict": "failed"}',
    )
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    assert conv.qa_offer_pending is True
    assert conv.pending_qa_report == '{"verdict": "failed"}'


# ── Project.product_type ───────────────────────────────────────────────────────


def test_project_product_type_defaults_none(db_session: Session, make_project: object) -> None:
    project = make_project()
    assert project.product_type is None


def test_project_product_type_persists(db_session: Session, make_project: object) -> None:
    project = make_project()
    project.product_type = "web_app"
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    assert project.product_type == "web_app"


# ── QASession ─────────────────────────────────────────────────────────────────


def test_qa_session_create(db_session: Session, make_project: object) -> None:
    project = make_project()
    session = QASession(
        project_id=project.id,
        triggered_by="user",
        status=QA_SESSION_STATUS_PENDING,
        product_type="web_app",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    assert session.id is not None
    assert session.project_id == project.id
    assert session.status == QA_SESSION_STATUS_PENDING
    assert session.product_type == "web_app"
    assert session.verdict is None


def test_qa_session_fixture(
    db_session: Session,
    make_project: object,
    make_qa_session: object,
) -> None:
    project = make_project()
    session = make_qa_session(project_id=project.id)
    assert session.id is not None
    assert session.status == QA_SESSION_STATUS_COMPLETED
    assert session.verdict == QA_VERDICT_PASSED


# ── QAFinding ─────────────────────────────────────────────────────────────────


def test_qa_finding_create(
    db_session: Session,
    make_project: object,
    make_qa_session: object,
) -> None:
    project = make_project()
    session = make_qa_session(project_id=project.id)
    finding = QAFinding(
        qa_session_id=session.id,
        finding_id="abc-123",
        severity="high",
        category="security",
        title="SQL injection in login",
        description="Login endpoint is vulnerable to SQL injection.",
        auto_remediable=True,
        remediation_hint="Use parameterised queries.",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(finding)
    db_session.commit()
    db_session.refresh(finding)

    assert finding.id is not None
    assert finding.qa_session_id == session.id
    assert finding.severity == "high"
    assert finding.auto_remediable is True


def test_qa_finding_fixture(
    db_session: Session,
    make_project: object,
    make_qa_session: object,
    make_qa_finding: object,
) -> None:
    project = make_project()
    session = make_qa_session(project_id=project.id)
    finding = make_qa_finding(qa_session_id=session.id)
    assert finding.id is not None
    assert finding.severity == "medium"


def test_qa_session_findings_relationship(
    db_session: Session,
    make_project: object,
    make_qa_session: object,
    make_qa_finding: object,
) -> None:
    project = make_project()
    session = make_qa_session(project_id=project.id)
    make_qa_finding(qa_session_id=session.id, finding_id="f1", title="Finding 1")
    make_qa_finding(qa_session_id=session.id, finding_id="f2", title="Finding 2")

    db_session.refresh(session)
    assert len(session.findings) == 2
