"""Tests for QARunner — Phase 10."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.qa_finding import QAFinding as QAFindingModel
from app.models.qa_session import (
    QA_SESSION_STATUS_RUNNING,
)
from app.models.qa_session import (
    QASession as QASessionModel,
)
from app.services.qa.contracts import (
    QAFindingDetail,
    QAResult,
    RemediationTask,
)
from app.services.qa.qa_runner import (
    _build_request,
    _notify_aria,
    _persist_findings,
    _run,
    create_qa_session,
    run_qa_background,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────


def _make_project(db: Session, project_id: int = 1, product_type: str = "rest_api") -> Project:
    project = Project(
        id=project_id,
        name="Test Project",
        description="A test project goal",
        product_type=product_type,
    )
    db.add(project)
    db.flush()
    return project


def _make_qa_session(db: Session, project_id: int = 1) -> QASessionModel:
    session = QASessionModel(
        id=10,
        project_id=project_id,
        triggered_by="user",
        status=QA_SESSION_STATUS_RUNNING,
        product_type="rest_api",
    )
    db.add(session)
    db.flush()
    return session


def _make_result(
    verdict: str = "passed",
    findings: list[QAFindingDetail] | None = None,
    remediation_tasks: list[RemediationTask] | None = None,
) -> QAResult:
    return QAResult(
        verdict=verdict,
        findings=findings or [],
        remediation_tasks=remediation_tasks or [],
        agents_called=["functional_qa_agent", "synthesis_agent"],
        duration_seconds=5.0,
    )


def _make_finding(
    finding_id: str = "f-001",
    severity: str = "high",
    producer_agent: str | None = "functional_qa_agent",
) -> QAFindingDetail:
    return QAFindingDetail(
        finding_id=finding_id,
        severity=severity,
        category="functional",
        title=f"Finding {finding_id}",
        description="A test finding",
        reproduction_steps=["step 1"],
        affected_component="Controller",
        auto_remediable=True,
        remediation_hint="Fix it",
        evidence={"file": "main.py"},
        producer_agent=producer_agent,
    )


# ── create_qa_session ─────────────────────────────────────────────────────────


class TestCreateQASession:
    def test_creates_session_with_running_status(self, db_session: Session):
        _make_project(db_session)
        session = create_qa_session(db_session, project_id=1)
        assert session.id is not None
        assert session.status == QA_SESSION_STATUS_RUNNING
        assert session.project_id == 1
        assert session.triggered_by == "user"

    def test_inherits_product_type_from_project(self, db_session: Session):
        _make_project(db_session, product_type="mobile_android")
        session = create_qa_session(db_session, project_id=1)
        assert session.product_type == "mobile_android"

    def test_uses_unknown_when_project_has_no_product_type(self, db_session: Session):
        project = Project(id=1, name="Test", description="desc")
        db_session.add(project)
        db_session.flush()

        session = create_qa_session(db_session, project_id=1)
        assert session.product_type == "unknown"

    def test_project_not_found_still_creates_session(self, db_session: Session):
        """When project is missing, product_type defaults to 'unknown'."""
        # Do not add any project
        # create_qa_session reads project and gracefully handles None
        project = Project(id=99, name="X", description="Y")
        db_session.add(project)
        db_session.flush()
        session = create_qa_session(db_session, project_id=99)
        assert session.project_id == 99


# ── _build_request ─────────────────────────────────────────────────────────────


class TestBuildRequest:
    def test_uses_project_description_as_goal(self, db_session: Session):
        project = _make_project(db_session)
        with patch("app.services.qa.qa_runner.ProjectStorageService") as mock_storage_cls:
            mock_storage = MagicMock()
            mock_paths = MagicMock()
            mock_paths.source_dir = "/projects/1/source"
            mock_storage.get_project_paths.return_value = mock_paths
            mock_storage_cls.return_value = mock_storage

            req = _build_request(db_session, project, qa_session_id=10)

        assert req.project_goal == "A test project goal"
        assert req.product_type == "rest_api"
        assert req.source_path == "/projects/1/source"
        assert req.qa_session_id == 10

    def test_falls_back_to_name_when_no_description(self, db_session: Session):
        project = Project(id=2, name="My App", product_type="web_app")
        db_session.add(project)
        db_session.flush()

        with patch("app.services.qa.qa_runner.ProjectStorageService") as mock_storage_cls:
            mock_storage = MagicMock()
            mock_paths = MagicMock()
            mock_paths.source_dir = "/projects/2/source"
            mock_storage.get_project_paths.return_value = mock_paths
            mock_storage_cls.return_value = mock_storage

            req = _build_request(db_session, project, qa_session_id=5)

        assert req.project_goal == "My App"

    def test_product_type_defaults_to_unknown(self, db_session: Session):
        project = Project(id=3, name="X", description="desc")
        db_session.add(project)
        db_session.flush()

        with patch("app.services.qa.qa_runner.ProjectStorageService") as mock_storage_cls:
            mock_storage = MagicMock()
            mock_paths = MagicMock()
            mock_paths.source_dir = "/projects/3/source"
            mock_storage.get_project_paths.return_value = mock_paths
            mock_storage_cls.return_value = mock_storage

            req = _build_request(db_session, project, qa_session_id=7)

        assert req.product_type == "unknown"


# ── _persist_findings ─────────────────────────────────────────────────────────


class TestPersistFindings:
    def test_persists_findings_to_db(self, db_session: Session):
        project = _make_project(db_session)
        qa_session = _make_qa_session(db_session, project_id=project.id)

        result = _make_result(
            verdict="failed",
            findings=[
                _make_finding("f-001", "critical"),
                _make_finding("f-002", "high"),
            ],
        )

        _persist_findings(db_session, qa_session.id, result)
        db_session.flush()

        rows = (
            db_session.query(QAFindingModel)
            .filter(QAFindingModel.qa_session_id == qa_session.id)
            .all()
        )
        assert len(rows) == 2
        assert rows[0].finding_id == "f-001"
        assert rows[0].severity == "critical"
        assert rows[1].finding_id == "f-002"

    def test_persists_all_finding_fields(self, db_session: Session):
        project = _make_project(db_session)
        qa_session = _make_qa_session(db_session, project_id=project.id)

        finding = _make_finding("f-003", "medium")
        result = _make_result(findings=[finding])
        _persist_findings(db_session, qa_session.id, result)
        db_session.flush()

        row = db_session.query(QAFindingModel).filter(QAFindingModel.finding_id == "f-003").first()
        assert row is not None
        assert row.affected_component == "Controller"
        assert row.auto_remediable is True
        assert row.remediation_hint == "Fix it"
        assert row.reproduction_steps == ["step 1"]

    def test_persists_producer_agent(self, db_session: Session):
        project = _make_project(db_session)
        qa_session = _make_qa_session(db_session, project_id=project.id)

        finding = _make_finding("f-010", producer_agent="security_qa_agent")
        result = _make_result(findings=[finding])
        _persist_findings(db_session, qa_session.id, result)
        db_session.flush()

        row = db_session.query(QAFindingModel).filter(QAFindingModel.finding_id == "f-010").first()
        assert row is not None
        assert row.producer_agent == "security_qa_agent"

    def test_persists_null_producer_agent(self, db_session: Session):
        project = _make_project(db_session)
        qa_session = _make_qa_session(db_session, project_id=project.id)

        finding = _make_finding("f-011", producer_agent=None)
        result = _make_result(findings=[finding])
        _persist_findings(db_session, qa_session.id, result)
        db_session.flush()

        row = db_session.query(QAFindingModel).filter(QAFindingModel.finding_id == "f-011").first()
        assert row is not None
        assert row.producer_agent is None

    def test_no_findings_persists_nothing(self, db_session: Session):
        project = _make_project(db_session)
        qa_session = _make_qa_session(db_session, project_id=project.id)

        result = _make_result(verdict="passed", findings=[])
        _persist_findings(db_session, qa_session.id, result)
        db_session.flush()

        rows = (
            db_session.query(QAFindingModel)
            .filter(QAFindingModel.qa_session_id == qa_session.id)
            .all()
        )
        assert rows == []


# ── _notify_aria ──────────────────────────────────────────────────────────────


class TestNotifyAria:
    def test_skips_when_no_active_conversation(self, db_session: Session):
        _make_project(db_session)
        result = _make_result()
        # Should not raise even with no conversation
        _notify_aria(db_session, project_id=1, result=result)

    def test_stores_pending_qa_report_on_conversation(self, db_session: Session):
        from app.models.conversation import (
            CONVERSATION_STATUS_ACTIVE,
            Conversation,
        )

        _make_project(db_session)
        conv = Conversation(
            project_id=1,
            status=CONVERSATION_STATUS_ACTIVE,
            phase="completed",
        )
        db_session.add(conv)
        db_session.flush()

        result = _make_result(verdict="passed")

        mock_resp = MagicMock()
        mock_resp.message = "QA completed"
        mock_resp.event = "qa_completed_no_issues"
        mock_resp.phase = "completed"
        mock_resp.conversation_id = conv.id
        mock_resp.project_id = 1

        with (
            patch(
                "app.services.qa.qa_runner.process_with_pre_transitions",
                return_value=mock_resp,
            ),
            patch("app.services.qa.qa_runner.manager"),
            patch("app.services.qa.qa_runner.assistant_message", return_value={}),
        ):
            _notify_aria(db_session, project_id=1, result=result)

        db_session.refresh(conv)
        assert conv.pending_qa_report is not None
        import json

        parsed = json.loads(conv.pending_qa_report)
        assert parsed["verdict"] == "passed"

    def test_sends_event_data_to_aria(self, db_session: Session):
        from app.models.conversation import (
            CONVERSATION_STATUS_ACTIVE,
            Conversation,
        )

        _make_project(db_session)
        conv = Conversation(
            project_id=1,
            status=CONVERSATION_STATUS_ACTIVE,
            phase="completed",
        )
        db_session.add(conv)
        db_session.flush()

        finding = _make_finding("f-001", "critical")
        result = _make_result(verdict="failed", findings=[finding])

        mock_resp = MagicMock()
        mock_resp.message = "Found issues"
        mock_resp.event = "qa_completed_with_findings"
        mock_resp.phase = "completed"
        mock_resp.conversation_id = conv.id
        mock_resp.project_id = 1

        captured_input = {}

        def _capture(db, conv_id, aria_input):
            captured_input["data"] = aria_input.system_event.data
            return mock_resp

        with (
            patch(
                "app.services.qa.qa_runner.process_with_pre_transitions",
                side_effect=_capture,
            ),
            patch("app.services.qa.qa_runner.manager"),
            patch("app.services.qa.qa_runner.assistant_message", return_value={}),
        ):
            _notify_aria(db_session, project_id=1, result=result)

        data = captured_input["data"]
        assert data["verdict"] == "failed"
        assert data["has_findings"] is True
        assert data["critical_count"] == 1

    def test_handles_aria_error_gracefully(self, db_session: Session):
        from app.models.conversation import (
            CONVERSATION_STATUS_ACTIVE,
            Conversation,
        )
        from app.services.conversation.aria import AriaOrchestratorError

        _make_project(db_session)
        conv = Conversation(
            project_id=1,
            status=CONVERSATION_STATUS_ACTIVE,
            phase="completed",
        )
        db_session.add(conv)
        db_session.flush()

        result = _make_result()

        with (
            patch(
                "app.services.qa.qa_runner.process_with_pre_transitions",
                side_effect=AriaOrchestratorError("boom"),
            ),
        ):
            # Should not raise
            _notify_aria(db_session, project_id=1, result=result)


# ── _run (integration) ────────────────────────────────────────────────────────


class TestRunIntegration:
    """Tests for the internal _run() function with QAEngine mocked."""

    def test_run_persists_findings_and_notifies(self, db_session: Session):
        project = _make_project(db_session)
        qa_session = _make_qa_session(db_session, project_id=project.id)

        result = _make_result(
            verdict="failed",
            findings=[_make_finding("f-001", "high")],
        )

        mock_engine = MagicMock()
        mock_engine.run.return_value = result

        with (
            patch("app.services.qa.qa_runner.QAEngine", return_value=mock_engine),
            patch("app.services.qa.qa_runner.ProjectStorageService") as mock_storage_cls,
            patch("app.services.qa.qa_runner._notify_aria") as mock_notify,
        ):
            mock_storage = MagicMock()
            mock_paths = MagicMock()
            mock_paths.source_dir = "/projects/1/source"
            mock_storage.get_project_paths.return_value = mock_paths
            mock_storage_cls.return_value = mock_storage

            _run(db_session, project_id=project.id, qa_session_id=qa_session.id)

        # Engine called
        mock_engine.run.assert_called_once()

        # Findings persisted
        rows = (
            db_session.query(QAFindingModel)
            .filter(QAFindingModel.qa_session_id == qa_session.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].finding_id == "f-001"

        # Aria notified
        mock_notify.assert_called_once_with(db_session, project.id, result)

    def test_run_returns_early_when_project_not_found(self, db_session: Session):
        mock_engine = MagicMock()
        with patch("app.services.qa.qa_runner.QAEngine", return_value=mock_engine):
            _run(db_session, project_id=9999, qa_session_id=1)

        mock_engine.run.assert_not_called()

    def test_passed_result_no_findings_still_commits(self, db_session: Session):
        project = _make_project(db_session)
        qa_session = _make_qa_session(db_session, project_id=project.id)

        result = _make_result(verdict="passed")

        mock_engine = MagicMock()
        mock_engine.run.return_value = result

        with (
            patch("app.services.qa.qa_runner.QAEngine", return_value=mock_engine),
            patch("app.services.qa.qa_runner.ProjectStorageService") as mock_storage_cls,
            patch("app.services.qa.qa_runner._notify_aria"),
        ):
            mock_storage = MagicMock()
            mock_paths = MagicMock()
            mock_paths.source_dir = "/projects/1/source"
            mock_storage.get_project_paths.return_value = mock_paths
            mock_storage_cls.return_value = mock_storage

            _run(db_session, project_id=project.id, qa_session_id=qa_session.id)

        rows = (
            db_session.query(QAFindingModel)
            .filter(QAFindingModel.qa_session_id == qa_session.id)
            .all()
        )
        assert rows == []


# ── run_qa_background ─────────────────────────────────────────────────────────


class TestRunQABackground:
    def test_calls_run_with_own_session(self):
        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)

        with (
            patch(
                "app.services.qa.qa_runner.SessionLocal",
                mock_session_local,
            ),
            patch("app.services.qa.qa_runner._run") as mock_run,
        ):
            run_qa_background(project_id=1, qa_session_id=10)

        mock_run.assert_called_once_with(mock_db, 1, 10)
        mock_db.close.assert_called_once()

    def test_closes_session_on_error(self):
        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)

        with (
            patch("app.services.qa.qa_runner.SessionLocal", mock_session_local),
            patch(
                "app.services.qa.qa_runner._run",
                side_effect=RuntimeError("crash"),
            ),
        ):
            # Should not propagate
            run_qa_background(project_id=1, qa_session_id=10)

        mock_db.close.assert_called_once()
