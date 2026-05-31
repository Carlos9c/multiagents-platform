"""End-to-end QA integration tests — Phase 12.

Covers the full pipeline for a cli_tool project without requiring Docker:

  1. create_qa_session  → QASession row with correct product_type
  2. _persist_findings  → QAFinding rows persisted from QAResult
  3. _notify_aria       → conversation.pending_qa_report set
  4. QATool.execute     → Task rows created when user confirms remediation

The QA engine itself (QAEngine.run) is mocked so these tests run without
Docker, live LLM calls, or real file-system access.  The Aria notification
path (process_with_pre_transitions + WebSocket broadcast) is also mocked so
only the DB side-effects are exercised.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_COMPLETED,
    CONVERSATION_PHASE_QA_RUNNING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.models.project import Project
from app.models.qa_finding import QAFinding as QAFindingModel
from app.models.qa_session import (
    QA_SESSION_STATUS_COMPLETED,
    QA_SESSION_STATUS_RUNNING,
)
from app.models.qa_session import (
    QASession as QASessionModel,
)
from app.models.task import PLANNING_LEVEL_ATOMIC, Task
from app.services.conversation.aria.tools.qa_tool import QATool
from app.services.qa.contracts import (
    QAFindingDetail,
    QAResult,
    RemediationTask,
)
from app.services.qa.qa_runner import (
    _notify_aria,
    _persist_findings,
    _run,
    create_qa_session,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_project(db: Session, product_type: str = "cli_tool") -> Project:
    project = Project(
        name="CLI Greeter",
        description="A greeting command-line tool",
        product_type=product_type,
    )
    db.add(project)
    db.flush()
    return project


def _make_conversation(
    db: Session,
    project_id: int,
    *,
    phase: str = CONVERSATION_PHASE_COMPLETED,
    pending_qa_report: str | None = None,
    qa_offer_pending: bool = False,
) -> Conversation:
    conv = Conversation(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=phase,
        review_episode_attempts=0,
        requirements_ready=False,
        qa_offer_pending=qa_offer_pending,
        pending_qa_report=pending_qa_report,
    )
    db.add(conv)
    db.flush()
    return conv


def _make_qa_result(
    verdict: str = "failed",
    *,
    n_findings: int = 2,
    n_remediation: int = 2,
) -> QAResult:
    findings = [
        QAFindingDetail(
            finding_id=f"fqa-CLI-{i:03d}",
            severity="high" if i == 0 else "medium",
            category="functional",
            title=f"CLI finding {i}",
            description=f"Functional issue {i} in the CLI tool.",
            reproduction_steps=[f"Run: ./greet --flag{i}"],
            affected_component="main.py",
            auto_remediable=False,
            remediation_hint=f"Fix the argument parsing for flag{i}.",
            producer_agent="functional_qa_agent",
        )
        for i in range(n_findings)
    ]
    remediations = [
        RemediationTask(
            finding_id=f"fqa-CLI-{i:03d}",
            title=f"Fix CLI issue {i}",
            description=f"Resolve argument parsing bug #{i}",
            priority="high" if i == 0 else "medium",
        )
        for i in range(n_remediation)
    ]
    return QAResult(
        verdict=verdict,
        findings=findings,
        remediation_tasks=remediations,
        agents_called=["functional_qa_agent", "boundary_qa_agent"],
        duration_seconds=18.5,
    )


def _patch_eval_yes():
    return patch(
        "app.services.conversation.aria.tools.qa_tool._evaluate_yes_no",
        return_value=True,
    )


# ── 1. create_qa_session ───────────────────────────────────────────────────────


class TestCreateQASessionCliTool:
    def test_stores_product_type_from_project(self, db_session: Session):
        project = _make_project(db_session, product_type="cli_tool")
        db_session.flush()

        session = create_qa_session(db_session, project.id)

        assert session.product_type == "cli_tool"

    def test_status_is_running(self, db_session: Session):
        project = _make_project(db_session)
        session = create_qa_session(db_session, project.id)

        assert session.status == QA_SESSION_STATUS_RUNNING

    def test_session_flushed_to_db(self, db_session: Session):
        project = _make_project(db_session)
        session = create_qa_session(db_session, project.id)
        db_session.flush()

        row = db_session.get(QASessionModel, session.id)
        assert row is not None
        assert row.project_id == project.id

    def test_triggered_by_user(self, db_session: Session):
        project = _make_project(db_session)
        session = create_qa_session(db_session, project.id)

        assert session.triggered_by == "user"


# ── 2. _persist_findings ──────────────────────────────────────────────────────


class TestPersistFindingsCliTool:
    def test_creates_finding_rows_for_each_finding(self, db_session: Session):
        project = _make_project(db_session)
        qa_session = QASessionModel(
            project_id=project.id,
            triggered_by="user",
            status=QA_SESSION_STATUS_RUNNING,
            product_type="cli_tool",
        )
        db_session.add(qa_session)
        db_session.flush()

        result = _make_qa_result(n_findings=2)
        _persist_findings(db_session, qa_session.id, result)
        db_session.flush()

        rows = (
            db_session.query(QAFindingModel)
            .filter(QAFindingModel.qa_session_id == qa_session.id)
            .all()
        )
        assert len(rows) == 2

    def test_finding_fields_are_persisted_correctly(self, db_session: Session):
        project = _make_project(db_session)
        qa_session = QASessionModel(
            project_id=project.id,
            triggered_by="user",
            status=QA_SESSION_STATUS_RUNNING,
            product_type="cli_tool",
        )
        db_session.add(qa_session)
        db_session.flush()

        result = _make_qa_result(n_findings=1)
        _persist_findings(db_session, qa_session.id, result)
        db_session.flush()

        row = (
            db_session.query(QAFindingModel)
            .filter(QAFindingModel.qa_session_id == qa_session.id)
            .first()
        )
        assert row is not None
        assert row.finding_id == "fqa-CLI-000"
        assert row.severity == "high"
        assert row.category == "functional"
        assert row.affected_component == "main.py"

    def test_zero_findings_creates_no_rows(self, db_session: Session):
        project = _make_project(db_session)
        qa_session = QASessionModel(
            project_id=project.id,
            triggered_by="user",
            status=QA_SESSION_STATUS_RUNNING,
            product_type="cli_tool",
        )
        db_session.add(qa_session)
        db_session.flush()

        result = _make_qa_result(verdict="passed", n_findings=0, n_remediation=0)
        _persist_findings(db_session, qa_session.id, result)
        db_session.flush()

        rows = (
            db_session.query(QAFindingModel)
            .filter(QAFindingModel.qa_session_id == qa_session.id)
            .all()
        )
        assert rows == []


# ── 3. _notify_aria ────────────────────────────────────────────────────────────


class TestNotifyAriaCliTool:
    def test_sets_pending_qa_report_on_conversation(self, db_session: Session):
        project = _make_project(db_session)
        conv = _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_QA_RUNNING)
        db_session.flush()

        result = _make_qa_result(verdict="failed", n_findings=2, n_remediation=2)

        with (
            patch("app.services.qa.qa_runner.process_with_pre_transitions") as mock_proc,
            patch("app.services.qa.qa_runner.manager"),
        ):
            mock_proc.return_value = MagicMock(
                message="QA done",
                event="qa_completed_with_findings",
                phase="completed",
                conversation_id=conv.id,
                project_id=project.id,
            )
            _notify_aria(db_session, project.id, result)

        db_session.refresh(conv)
        assert conv.pending_qa_report is not None
        parsed = json.loads(conv.pending_qa_report)
        assert parsed["verdict"] == "failed"
        assert len(parsed["findings"]) == 2

    def test_pending_report_contains_remediation_tasks(self, db_session: Session):
        project = _make_project(db_session)
        conv = _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_QA_RUNNING)
        db_session.flush()

        result = _make_qa_result(n_remediation=2)

        with (
            patch("app.services.qa.qa_runner.process_with_pre_transitions") as mock_proc,
            patch("app.services.qa.qa_runner.manager"),
        ):
            mock_proc.return_value = MagicMock(
                message="ok",
                event="qa_completed_with_findings",
                phase="completed",
                conversation_id=conv.id,
                project_id=project.id,
            )
            _notify_aria(db_session, project.id, result)

        db_session.refresh(conv)
        parsed = json.loads(conv.pending_qa_report)
        assert len(parsed["remediation_tasks"]) == 2
        assert parsed["remediation_tasks"][0]["finding_id"] == "fqa-CLI-000"

    def test_does_nothing_when_no_active_conversation(self, db_session: Session):
        project = _make_project(db_session)
        # No conversation created
        db_session.flush()

        result = _make_qa_result()

        # Should complete without error, no DB changes
        _notify_aria(db_session, project.id, result)


# ── 4. QATool remediation from QA report ──────────────────────────────────────


class TestQAToolRemediationCliTool:
    def test_user_confirms_creates_task_rows(self, db_session: Session):
        """Confirming remediation creates Task ORM rows with correct fields."""
        project = _make_project(db_session)
        result = _make_qa_result(n_findings=2, n_remediation=2)
        conv = _make_conversation(
            db_session,
            project.id,
            pending_qa_report=result.model_dump_json(),
        )
        db_session.flush()

        with _patch_eval_yes():
            tool_result = QATool().execute(db_session, project.id, conv, hint="sí, créalas")

        assert tool_result.data["status"] == "remediation_confirmed"
        assert tool_result.data["tasks_created"] == 2

        tasks = db_session.query(Task).filter(Task.project_id == project.id).order_by(Task.id).all()
        assert len(tasks) == 2
        assert tasks[0].title == "Fix CLI issue 0"
        assert tasks[0].planning_level == PLANNING_LEVEL_ATOMIC
        assert tasks[0].priority == "high"
        assert tasks[1].title == "Fix CLI issue 1"
        assert tasks[1].priority == "medium"

    def test_task_rows_have_correct_description(self, db_session: Session):
        project = _make_project(db_session)
        result = _make_qa_result(n_findings=1, n_remediation=1)
        conv = _make_conversation(
            db_session,
            project.id,
            pending_qa_report=result.model_dump_json(),
        )
        db_session.flush()

        with _patch_eval_yes():
            QATool().execute(db_session, project.id, conv, hint="sí")

        task = db_session.query(Task).filter(Task.project_id == project.id).first()
        assert task is not None
        assert "argument parsing bug" in task.description

    def test_zero_remediation_tasks_creates_zero_rows(self, db_session: Session):
        project = _make_project(db_session)
        result = _make_qa_result(verdict="passed", n_findings=0, n_remediation=0)
        conv = _make_conversation(
            db_session,
            project.id,
            pending_qa_report=result.model_dump_json(),
        )
        db_session.flush()

        with _patch_eval_yes():
            tool_result = QATool().execute(db_session, project.id, conv, hint="sí")

        assert tool_result.data["tasks_created"] == 0
        tasks = db_session.query(Task).filter(Task.project_id == project.id).all()
        assert tasks == []

    def test_declined_creates_no_tasks(self, db_session: Session):
        project = _make_project(db_session)
        result = _make_qa_result(n_remediation=2)
        conv = _make_conversation(
            db_session,
            project.id,
            pending_qa_report=result.model_dump_json(),
        )
        db_session.flush()

        with patch(
            "app.services.conversation.aria.tools.qa_tool._evaluate_yes_no",
            return_value=False,
        ):
            tool_result = QATool().execute(db_session, project.id, conv, hint="no")

        assert tool_result.data["status"] == "remediation_declined"
        tasks = db_session.query(Task).filter(Task.project_id == project.id).all()
        assert tasks == []


# ── 5. Full pipeline: _run wires QAEngine → findings → notify ─────────────────


class TestFullQAPipelineCliTool:
    def test_run_persists_findings_and_notifies(self, db_session: Session):
        """_run calls the engine, persists findings, and notifies Aria."""
        project = _make_project(db_session)
        qa_session = QASessionModel(
            project_id=project.id,
            triggered_by="user",
            status=QA_SESSION_STATUS_RUNNING,
            product_type="cli_tool",
        )
        db_session.add(qa_session)
        conv = _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_QA_RUNNING)
        db_session.flush()

        engine_result = _make_qa_result(verdict="failed", n_findings=2, n_remediation=2)

        with (
            patch("app.services.qa.qa_runner.QAEngine") as mock_engine_cls,
            patch("app.services.qa.qa_runner.process_with_pre_transitions") as mock_proc,
            patch("app.services.qa.qa_runner.manager"),
            patch("app.services.qa.qa_runner.ProjectStorageService") as mock_storage,
        ):
            mock_engine = MagicMock()
            mock_engine.run.return_value = engine_result
            mock_engine_cls.return_value = mock_engine

            mock_proc.return_value = MagicMock(
                message="ok",
                event="qa_completed_with_findings",
                phase="completed",
                conversation_id=conv.id,
                project_id=project.id,
            )

            mock_paths = MagicMock()
            mock_paths.source_dir = "/tmp/project"
            mock_storage.return_value.get_project_paths.return_value = mock_paths

            _run(db_session, project.id, qa_session.id)

        # Findings must be persisted
        findings = (
            db_session.query(QAFindingModel)
            .filter(QAFindingModel.qa_session_id == qa_session.id)
            .all()
        )
        assert len(findings) == 2

        # pending_qa_report must be set
        db_session.refresh(conv)
        assert conv.pending_qa_report is not None
        parsed = json.loads(conv.pending_qa_report)
        assert parsed["verdict"] == "failed"

    def test_run_updates_qa_session_status_to_completed(self, db_session: Session):
        """QAEngine._persist_result sets QASession status=completed after the run."""
        project = _make_project(db_session)
        qa_session = QASessionModel(
            project_id=project.id,
            triggered_by="user",
            status=QA_SESSION_STATUS_RUNNING,
            product_type="cli_tool",
        )
        db_session.add(qa_session)
        db_session.flush()

        engine_result = _make_qa_result(verdict="passed", n_findings=0, n_remediation=0)

        # Use a real QAEngine so _persist_result runs, but mock bootstrapper/orchestrator
        from app.services.qa.qa_engine import QAEngine

        with (
            patch.object(QAEngine, "_run_internal", return_value=engine_result),
            patch("app.services.qa.qa_runner.process_with_pre_transitions") as mock_proc,
            patch("app.services.qa.qa_runner.manager"),
            patch("app.services.qa.qa_runner.ProjectStorageService") as mock_storage,
        ):
            mock_proc.return_value = MagicMock(
                message="ok",
                event="qa_completed_no_issues",
                phase="completed",
                conversation_id=1,
                project_id=project.id,
            )
            mock_paths = MagicMock()
            mock_paths.source_dir = "/tmp/project"
            mock_storage.return_value.get_project_paths.return_value = mock_paths

            _run(db_session, project.id, qa_session.id)

        db_session.refresh(qa_session)
        assert qa_session.status == QA_SESSION_STATUS_COMPLETED
        assert qa_session.verdict == "passed"
