"""Tests for QATool — Phase 11."""

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
from app.models.task import PLANNING_LEVEL_ATOMIC, Task
from app.services.conversation.aria.contracts import ToolName
from app.services.conversation.aria.tools.qa_tool import QATool

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_conv(
    db: Session,
    project_id: int,
    *,
    phase: str = CONVERSATION_PHASE_COMPLETED,
    qa_offer_pending: bool = False,
    pending_qa_report: str | None = None,
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


def _qa_result_json(
    verdict: str = "passed",
    findings: list | None = None,
    remediation_tasks: list | None = None,
    findings_summary: dict | None = None,
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "findings": findings or [],
            "findings_summary": findings_summary
            or {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "remediation_tasks": remediation_tasks or [],
            "probes": [],
            "agents_called": ["functional_qa_agent"],
            "duration_seconds": 5.0,
            "error_message": None,
        }
    )


def _patch_eval_yes():
    return patch(
        "app.services.conversation.aria.tools.qa_tool._evaluate_yes_no",
        return_value=True,
    )


def _patch_eval_no():
    return patch(
        "app.services.conversation.aria.tools.qa_tool._evaluate_yes_no",
        return_value=False,
    )


# ── Tool name ─────────────────────────────────────────────────────────────────


def test_tool_name():
    assert QATool().name == ToolName.QA


# ── Situation B: QA running ───────────────────────────────────────────────────


class TestSituationB:
    def test_returns_qa_running_status(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_QA_RUNNING)

        result = QATool().execute(db_session, project.id, conv)

        assert result.tool_name == ToolName.QA
        assert result.data["status"] == "qa_running"

    def test_returns_qa_running_even_with_hint(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_QA_RUNNING)

        result = QATool().execute(db_session, project.id, conv, hint="¿Cuánto falta?")

        assert result.data["status"] == "qa_running"

    def test_no_llm_called_for_qa_running(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_QA_RUNNING)

        with patch("app.services.conversation.aria.tools.qa_tool.get_llm_provider") as mock_llm:
            QATool().execute(db_session, project.id, conv)

        mock_llm.assert_not_called()


# ── Situation C: QA completed, no hint — summarise ───────────────────────────


class TestSituationC:
    def test_no_findings_returns_no_issues_status(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report=_qa_result_json("passed"),
        )

        result = QATool().execute(db_session, project.id, conv)

        assert result.data["status"] == "qa_completed_no_issues"
        assert result.data["verdict"] == "passed"
        assert result.data["has_findings"] is False

    def test_with_findings_returns_with_findings_status(self, db_session: Session, make_project):
        project = make_project()
        findings = [
            {
                "finding_id": "f-001",
                "severity": "high",
                "category": "security",
                "title": "SQL injection",
                "description": "desc",
            },
        ]
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report=_qa_result_json(
                "failed",
                findings=findings,
                findings_summary={"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            ),
        )

        result = QATool().execute(db_session, project.id, conv)

        assert result.data["status"] == "qa_completed_with_findings"
        assert result.data["has_findings"] is True
        assert result.data["total_findings"] == 1

    def test_with_remediation_tasks_sets_has_remediation(self, db_session: Session, make_project):
        project = make_project()
        remediation = [
            {
                "finding_id": "f-001",
                "title": "Fix SQL injection",
                "description": "Use parameterized queries",
                "priority": "high",
            },
        ]
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report=_qa_result_json("failed", remediation_tasks=remediation),
        )

        result = QATool().execute(db_session, project.id, conv)

        assert result.data["has_remediation_tasks"] is True
        assert len(result.data["remediation_tasks"]) == 1

    def test_no_llm_called_for_summary(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report=_qa_result_json("passed"),
        )

        with patch("app.services.conversation.aria.tools.qa_tool.get_llm_provider") as mock_llm:
            QATool().execute(db_session, project.id, conv)

        mock_llm.assert_not_called()

    def test_malformed_report_returns_gracefully(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report="not-valid-json",
        )

        result = QATool().execute(db_session, project.id, conv)

        assert result.data["status"] == "qa_completed_no_issues"
        assert "error" in result.data

    def test_returns_findings_summary_dict(self, db_session: Session, make_project):
        project = make_project()
        summary = {"critical": 1, "high": 2, "medium": 0, "low": 0, "info": 0}
        findings = [
            {
                "finding_id": f"f-{i}",
                "severity": "high",
                "category": "functional",
                "title": f"Issue {i}",
                "description": "desc",
            }
            for i in range(2)
        ] + [
            {
                "finding_id": "f-99",
                "severity": "critical",
                "category": "security",
                "title": "Crit",
                "description": "desc",
            }
        ]
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report=_qa_result_json(
                "failed", findings=findings, findings_summary=summary
            ),
        )

        result = QATool().execute(db_session, project.id, conv)

        assert result.data["findings_summary"] == summary


# ── Situation A1: offer pending, no hint — make offer ─────────────────────────


class TestSituationA1:
    def test_returns_qa_offer_status(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(db_session, project.id, qa_offer_pending=True)

        result = QATool().execute(db_session, project.id, conv)

        assert result.data["status"] == "qa_offer"

    def test_includes_project_name(self, db_session: Session, make_project):
        project = make_project(name="My API Project")
        conv = _make_conv(db_session, project.id, qa_offer_pending=True)

        result = QATool().execute(db_session, project.id, conv)

        assert result.data["project_name"] == "My API Project"

    def test_includes_task_counts(self, db_session: Session, make_project):
        from app.models.task import TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, Task

        project = make_project()
        for i in range(3):
            db_session.add(
                Task(
                    project_id=project.id,
                    title=f"Task {i}",
                    planning_level=PLANNING_LEVEL_ATOMIC,
                    status=TASK_STATUS_COMPLETED,
                )
            )
        db_session.add(
            Task(
                project_id=project.id,
                title="Failed task",
                planning_level=PLANNING_LEVEL_ATOMIC,
                status=TASK_STATUS_FAILED,
            )
        )
        db_session.flush()

        conv = _make_conv(db_session, project.id, qa_offer_pending=True)
        result = QATool().execute(db_session, project.id, conv)

        assert result.data["completed_tasks"] == 3
        assert result.data["failed_tasks"] == 1

    def test_no_llm_called_for_offer(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(db_session, project.id, qa_offer_pending=True)

        with patch("app.services.conversation.aria.tools.qa_tool.get_llm_provider") as mock_llm:
            QATool().execute(db_session, project.id, conv)

        mock_llm.assert_not_called()


# ── Situation A2: offer pending, hint — respond to offer ─────────────────────


class TestSituationA2:
    def test_user_accepts_launches_qa(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(db_session, project.id, qa_offer_pending=True)

        mock_session = MagicMock()
        mock_session.id = 42

        with (
            _patch_eval_yes(),
            # Late import inside method — patch at source
            patch(
                "app.services.qa.qa_runner.create_qa_session",
                return_value=mock_session,
            ),
            patch("app.services.conversation.aria.tools.qa_tool._qa_executor"),
        ):
            result = QATool().execute(db_session, project.id, conv, hint="sí, ejecuta")

        assert result.data["status"] == "qa_started"
        assert result.data["qa_session_id"] == 42

    def test_user_declines_returns_declined(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(db_session, project.id, qa_offer_pending=True)

        with _patch_eval_no():
            result = QATool().execute(db_session, project.id, conv, hint="no gracias")

        assert result.data["status"] == "qa_offer_declined"

    def test_acceptance_submits_to_executor(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(db_session, project.id, qa_offer_pending=True)

        mock_session = MagicMock()
        mock_session.id = 7

        with (
            _patch_eval_yes(),
            patch(
                "app.services.qa.qa_runner.create_qa_session",
                return_value=mock_session,
            ),
            patch("app.services.conversation.aria.tools.qa_tool._qa_executor") as mock_exec,
        ):
            QATool().execute(db_session, project.id, conv, hint="adelante")

        mock_exec.submit.assert_called_once()

    def test_llm_evaluates_user_response(self, db_session: Session, make_project):
        """_evaluate_yes_no is called with the hint."""
        project = make_project()
        conv = _make_conv(db_session, project.id, qa_offer_pending=True)

        mock_session = MagicMock()
        mock_session.id = 1

        captured_args: list = []

        def _capture(context, user_response):
            captured_args.append(user_response)
            return True

        with (
            patch(
                "app.services.conversation.aria.tools.qa_tool._evaluate_yes_no",
                side_effect=_capture,
            ),
            patch(
                "app.services.qa.qa_runner.create_qa_session",
                return_value=mock_session,
            ),
            patch("app.services.conversation.aria.tools.qa_tool._qa_executor"),
        ):
            QATool().execute(db_session, project.id, conv, hint="sí claro")

        assert captured_args == ["sí claro"]


# ── Situation D: pending_qa_report + hint — remediation response ──────────────


class TestSituationD:
    def test_user_confirms_creates_tasks(self, db_session: Session, make_project):
        project = make_project()
        remediation = [
            {
                "finding_id": "f-001",
                "title": "Fix SQL injection",
                "description": "Use parameterized queries",
                "priority": "high",
            },
            {
                "finding_id": "f-002",
                "title": "Add rate limiting",
                "description": "Throttle the API",
                "priority": "medium",
            },
        ]
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report=_qa_result_json("failed", remediation_tasks=remediation),
        )

        with _patch_eval_yes():
            result = QATool().execute(db_session, project.id, conv, hint="sí, créalas")

        assert result.data["status"] == "remediation_confirmed"
        assert result.data["tasks_created"] == 2

        tasks = db_session.query(Task).filter(Task.project_id == project.id).all()
        assert len(tasks) == 2
        assert tasks[0].title == "Fix SQL injection"
        assert tasks[0].planning_level == PLANNING_LEVEL_ATOMIC
        assert tasks[0].priority == "high"

    def test_user_declines_returns_declined(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report=_qa_result_json("failed"),
        )

        with _patch_eval_no():
            result = QATool().execute(db_session, project.id, conv, hint="no, sin tareas")

        assert result.data["status"] == "remediation_declined"

    def test_declined_creates_no_tasks(self, db_session: Session, make_project):
        project = make_project()
        remediation = [
            {"finding_id": "f-001", "title": "Fix X", "description": "Do X", "priority": "low"},
        ]
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report=_qa_result_json("failed", remediation_tasks=remediation),
        )

        with _patch_eval_no():
            QATool().execute(db_session, project.id, conv, hint="no")

        tasks = db_session.query(Task).filter(Task.project_id == project.id).all()
        assert tasks == []

    def test_no_remediation_tasks_creates_zero(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report=_qa_result_json("failed", remediation_tasks=[]),
        )

        with _patch_eval_yes():
            result = QATool().execute(db_session, project.id, conv, hint="sí")

        assert result.data["status"] == "remediation_confirmed"
        assert result.data["tasks_created"] == 0

    def test_confirmed_with_side_effects_summary(self, db_session: Session, make_project):
        project = make_project()
        remediation = [
            {"finding_id": "f-001", "title": "Fix A", "description": "...", "priority": "high"},
        ]
        conv = _make_conv(
            db_session,
            project.id,
            pending_qa_report=_qa_result_json("failed", remediation_tasks=remediation),
        )

        with _patch_eval_yes():
            result = QATool().execute(db_session, project.id, conv, hint="sí")

        assert result.side_effects_summary is not None
        assert "1" in result.side_effects_summary


# ── Fallback: no active QA state ─────────────────────────────────────────────


class TestFallback:
    def test_returns_no_action_when_nothing_active(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(
            db_session,
            project.id,
            qa_offer_pending=False,
            pending_qa_report=None,
        )

        result = QATool().execute(db_session, project.id, conv)

        assert result.data["status"] == "no_action"
