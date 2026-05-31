"""Tests for QAEngine — Phase 4."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.qa_session import QA_SESSION_STATUS_COMPLETED
from app.models.qa_session import QASession as QASessionModel
from app.services.qa.contracts import (
    QA_VERDICT_BLOCKED,
    QARequest,
    QAResult,
)
from app.services.qa.qa_bootstrapper import QABootstrapper
from app.services.qa.qa_engine import QAEngine

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_request(product_type: str = "rest_api") -> QARequest:
    return QARequest(
        project_id=1,
        qa_session_id=99,
        product_type=product_type,
        project_goal="Build something",
        source_path="/projects/1/source",
        workspace_path="/projects/1/workspace",
    )


def _make_db_qa_session(qa_session_id: int = 99) -> MagicMock:
    m = MagicMock(spec=QASessionModel)
    m.id = qa_session_id
    m.strategy_used = None
    return m


def _make_passed_result() -> QAResult:
    return QAResult(verdict="passed", agents_called=["synthesis_agent"], duration_seconds=0.5)


def _make_blocked_result(msg: str = "blocked") -> QAResult:
    return QAResult(verdict="blocked", error_message=msg, duration_seconds=0.1)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_engine_returns_result_from_orchestrator(db_session: Session, make_project):
    project = make_project()
    from datetime import datetime, timezone

    from app.models.qa_session import (
        QA_SESSION_STATUS_PENDING,
        QA_VERDICT_PASSED,
    )
    from app.models.qa_session import (
        QASession as QASessionModel,
    )

    qa_sess = QASessionModel(
        project_id=project.id,
        triggered_by="user",
        status=QA_SESSION_STATUS_PENDING,
        product_type="rest_api",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(qa_sess)
    db_session.commit()
    db_session.refresh(qa_sess)

    request = QARequest(
        project_id=project.id,
        qa_session_id=qa_sess.id,
        product_type="rest_api",
        project_goal="Build a REST API",
        source_path="/projects/1/source",
        workspace_path="/projects/1/workspace",
    )

    mock_bootstrapper = MagicMock(spec=QABootstrapper)
    mock_bootstrapper.bootstrap.return_value = (request, None)

    with patch(
        "app.services.qa.qa_engine.QAOrchestrator.run",
        return_value=_make_passed_result(),
    ):
        engine = QAEngine(bootstrapper=mock_bootstrapper)
        result = engine.run(db=db_session, request=request)

    assert result.verdict == QA_VERDICT_PASSED
    db_session.refresh(qa_sess)
    assert qa_sess.status == QA_SESSION_STATUS_COMPLETED
    assert qa_sess.verdict == QA_VERDICT_PASSED


def test_engine_returns_blocked_when_bootstrap_fails(db_session: Session, make_project):
    project = make_project()
    from datetime import datetime, timezone

    from app.models.qa_session import QA_SESSION_STATUS_PENDING
    from app.models.qa_session import QASession as QASessionModel

    qa_sess = QASessionModel(
        project_id=project.id,
        triggered_by="user",
        status=QA_SESSION_STATUS_PENDING,
        product_type="mobile_android",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(qa_sess)
    db_session.commit()
    db_session.refresh(qa_sess)

    request = QARequest(
        project_id=project.id,
        qa_session_id=qa_sess.id,
        product_type="mobile_android",
        project_goal="Build an Android app",
        source_path="/projects/1/source",
        workspace_path="/projects/1/workspace",
    )

    mock_bootstrapper = MagicMock(spec=QABootstrapper)
    mock_bootstrapper.bootstrap.return_value = (
        request,
        _make_blocked_result("APK compilation failed"),
    )

    engine = QAEngine(bootstrapper=mock_bootstrapper)
    result = engine.run(db=db_session, request=request)

    assert result.verdict == QA_VERDICT_BLOCKED
    assert "APK compilation failed" in (result.error_message or "")


def test_engine_never_raises_on_internal_error(db_session: Session, make_project):
    project = make_project()
    request = QARequest(
        project_id=project.id,
        qa_session_id=9999,  # non-existent QASession
        product_type="rest_api",
        project_goal="Build something",
        source_path="/projects/1/source",
        workspace_path="/projects/1/workspace",
    )

    mock_bootstrapper = MagicMock(spec=QABootstrapper)
    mock_bootstrapper.bootstrap.side_effect = RuntimeError("Unexpected crash")

    engine = QAEngine(bootstrapper=mock_bootstrapper)
    result = engine.run(db=db_session, request=request)

    # Must not raise; returns blocked verdict
    assert result.verdict == QA_VERDICT_BLOCKED
    assert result.error_message is not None


def test_engine_uses_custom_budget():
    from app.services.qa.budget import QABudget

    engine = QAEngine(budget=QABudget(max_steps=3, max_agent_calls=2))
    assert engine._budget.max_steps == 3
    assert engine._budget.max_agent_calls == 2
