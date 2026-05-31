"""Tests for QA-related orchestrator transitions — Phase 11.

Covers:
- apply_system_event_pre_loop for PROJECT_COMPLETED (eligible / not eligible)
- apply_system_event_pre_loop for QA_COMPLETED (phase + pending_qa_report)
- _apply_phase_transitions for QA tool statuses
- _determine_event for QA-related situations
- Full process_with_pre_transitions integration paths
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_COMPLETED,
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_QA_RUNNING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.models.project import Project
from app.services.conversation.aria.contracts import (
    AriaInput,
    AriaStep,
    SystemEvent,
    SystemEventType,
    ToolName,
    ToolResult,
)
from app.services.conversation.aria.orchestrator import (
    _apply_phase_transitions,
    _determine_event,
    apply_system_event_pre_loop,
    process_with_pre_transitions,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_project(db: Session, product_type: str = "rest_api") -> Project:
    project = Project(name="Test Project", description="desc", product_type=product_type)
    db.add(project)
    db.flush()
    return project


def _make_conv(
    db: Session,
    project_id: int,
    *,
    phase: str = CONVERSATION_PHASE_EXECUTING,
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
    db.commit()
    db.refresh(conv)
    return conv


def _aria_step_respond(msg: str = "OK") -> AriaStep:
    return AriaStep(action="respond", response=msg, reasoning="test")


def _aria_step_call_qa(hint: str | None = None) -> AriaStep:
    return AriaStep(action="call_tool", tool_name=ToolName.QA, tool_hint=hint, reasoning="test")


def _qa_tool_result(status: str, **kwargs) -> ToolResult:
    return ToolResult(tool_name=ToolName.QA, data={"status": status, **kwargs})


# ── apply_system_event_pre_loop — PROJECT_COMPLETED ───────────────────────────


class TestPreLoopProjectCompleted:
    def test_sets_phase_completed(self, db_session: Session):
        project = _make_project(db_session, product_type="rest_api")
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        aria_input = AriaInput(
            source="system",
            system_event=SystemEvent(
                event_type=SystemEventType.PROJECT_COMPLETED,
                data={"completed": 5, "failed": 0},
            ),
        )
        apply_system_event_pre_loop(db_session, conv, aria_input)

        assert conv.phase == CONVERSATION_PHASE_COMPLETED

    def test_sets_qa_offer_pending_for_eligible_product(self, db_session: Session):
        project = _make_project(db_session, product_type="rest_api")
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        aria_input = AriaInput(
            source="system",
            system_event=SystemEvent(
                event_type=SystemEventType.PROJECT_COMPLETED,
                data={},
            ),
        )
        apply_system_event_pre_loop(db_session, conv, aria_input)

        assert conv.qa_offer_pending is True

    def test_does_not_set_qa_offer_for_ineligible_product(self, db_session: Session):
        project = _make_project(db_session, product_type="written_content")
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        aria_input = AriaInput(
            source="system",
            system_event=SystemEvent(
                event_type=SystemEventType.PROJECT_COMPLETED,
                data={},
            ),
        )
        apply_system_event_pre_loop(db_session, conv, aria_input)

        assert conv.qa_offer_pending is False

    def test_does_not_set_qa_offer_when_product_type_none(self, db_session: Session):
        project = _make_project(db_session, product_type=None)
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        aria_input = AriaInput(
            source="system",
            system_event=SystemEvent(
                event_type=SystemEventType.PROJECT_COMPLETED,
                data={},
            ),
        )
        apply_system_event_pre_loop(db_session, conv, aria_input)

        assert conv.qa_offer_pending is False

    @pytest.mark.parametrize("product_type", ["web_app", "rest_api", "cli_tool"])
    def test_eligible_product_types(self, db_session: Session, product_type: str):
        project = _make_project(db_session, product_type=product_type)
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        aria_input = AriaInput(
            source="system",
            system_event=SystemEvent(event_type=SystemEventType.PROJECT_COMPLETED, data={}),
        )
        apply_system_event_pre_loop(db_session, conv, aria_input)

        assert conv.qa_offer_pending is True


# ── apply_system_event_pre_loop — QA_COMPLETED ───────────────────────────────


class TestPreLoopQACompleted:
    def test_transitions_qa_running_to_completed(self, db_session: Session):
        project = _make_project(db_session)
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_QA_RUNNING)

        aria_input = AriaInput(
            source="system",
            system_event=SystemEvent(
                event_type=SystemEventType.QA_COMPLETED,
                data={"verdict": "passed"},
            ),
        )
        apply_system_event_pre_loop(db_session, conv, aria_input)

        assert conv.phase == CONVERSATION_PHASE_COMPLETED

    def test_does_not_change_phase_if_not_qa_running(self, db_session: Session):
        project = _make_project(db_session)
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_COMPLETED)

        aria_input = AriaInput(
            source="system",
            system_event=SystemEvent(
                event_type=SystemEventType.QA_COMPLETED,
                data={"verdict": "passed"},
            ),
        )
        apply_system_event_pre_loop(db_session, conv, aria_input)

        assert conv.phase == CONVERSATION_PHASE_COMPLETED

    def test_writes_pending_qa_report_when_empty(self, db_session: Session):
        project = _make_project(db_session)
        conv = _make_conv(
            db_session,
            project.id,
            phase=CONVERSATION_PHASE_QA_RUNNING,
            pending_qa_report=None,
        )

        aria_input = AriaInput(
            source="system",
            system_event=SystemEvent(
                event_type=SystemEventType.QA_COMPLETED,
                data={"verdict": "failed", "has_findings": True},
            ),
        )
        apply_system_event_pre_loop(db_session, conv, aria_input)

        assert conv.pending_qa_report is not None
        parsed = json.loads(conv.pending_qa_report)
        assert parsed["verdict"] == "failed"

    def test_does_not_overwrite_existing_pending_qa_report(self, db_session: Session):
        project = _make_project(db_session)
        original_report = json.dumps({"verdict": "passed", "findings": []})
        conv = _make_conv(
            db_session,
            project.id,
            phase=CONVERSATION_PHASE_QA_RUNNING,
            pending_qa_report=original_report,
        )

        aria_input = AriaInput(
            source="system",
            system_event=SystemEvent(
                event_type=SystemEventType.QA_COMPLETED,
                data={"verdict": "failed"},  # different from what's stored
            ),
        )
        apply_system_event_pre_loop(db_session, conv, aria_input)

        # Should keep the original report unchanged
        assert conv.pending_qa_report == original_report


# ── _apply_phase_transitions — QA tool results ───────────────────────────────


class TestPhaseTransitionsQA:
    def _conv(self, db: Session, project_id: int, **kwargs) -> Conversation:
        conv = Conversation(
            project_id=project_id,
            status=CONVERSATION_STATUS_ACTIVE,
            phase=CONVERSATION_PHASE_COMPLETED,
            review_episode_attempts=0,
            requirements_ready=False,
            **kwargs,
        )
        db.add(conv)
        db.flush()
        return conv

    def test_qa_started_sets_qa_running_and_clears_offer(self, db_session: Session, make_project):
        project = make_project()
        conv = self._conv(db_session, project.id, qa_offer_pending=True)

        _apply_phase_transitions(
            db_session,
            conv,
            _aria_step_respond(),
            [_qa_tool_result("qa_started", qa_session_id=10)],
        )

        assert conv.phase == CONVERSATION_PHASE_QA_RUNNING
        assert conv.qa_offer_pending is False

    def test_qa_offer_declined_clears_offer_pending(self, db_session: Session, make_project):
        project = make_project()
        conv = self._conv(db_session, project.id, qa_offer_pending=True)

        _apply_phase_transitions(
            db_session,
            conv,
            _aria_step_respond(),
            [_qa_tool_result("qa_offer_declined")],
        )

        assert conv.qa_offer_pending is False
        assert conv.phase == CONVERSATION_PHASE_COMPLETED  # unchanged

    def test_remediation_confirmed_sets_executing_and_clears_report(
        self, db_session: Session, make_project
    ):
        project = make_project()
        conv = self._conv(
            db_session,
            project.id,
            pending_qa_report='{"verdict": "failed"}',
        )

        _apply_phase_transitions(
            db_session,
            conv,
            _aria_step_respond(),
            [_qa_tool_result("remediation_confirmed", tasks_created=2)],
        )

        assert conv.phase == CONVERSATION_PHASE_EXECUTING
        assert conv.pending_qa_report is None

    def test_remediation_declined_clears_report_keeps_completed(
        self, db_session: Session, make_project
    ):
        project = make_project()
        conv = self._conv(
            db_session,
            project.id,
            pending_qa_report='{"verdict": "failed"}',
        )

        _apply_phase_transitions(
            db_session,
            conv,
            _aria_step_respond(),
            [_qa_tool_result("remediation_declined")],
        )

        assert conv.phase == CONVERSATION_PHASE_COMPLETED
        assert conv.pending_qa_report is None

    def test_qa_offer_status_no_state_change(self, db_session: Session, make_project):
        """qa_offer status only provides data — no phase transition."""
        project = make_project()
        conv = self._conv(db_session, project.id, qa_offer_pending=True)

        _apply_phase_transitions(
            db_session,
            conv,
            _aria_step_respond(),
            [_qa_tool_result("qa_offer", project_name="X")],
        )

        assert conv.phase == CONVERSATION_PHASE_COMPLETED
        assert conv.qa_offer_pending is True  # untouched


# ── _determine_event — QA tool results ───────────────────────────────────────


class TestDetermineEventQA:
    def _conv_stub(self, phase: str = CONVERSATION_PHASE_COMPLETED) -> MagicMock:
        conv = MagicMock()
        conv.phase = phase
        conv.pending_qa_report = None
        conv.qa_offer_pending = False
        return conv

    def test_qa_started_returns_qa_running(self):
        event = _determine_event(
            tool_results=[_qa_tool_result("qa_started")],
            is_fallback=False,
        )
        assert event == "qa_running"

    def test_qa_offer_declined_returns_project_completed(self):
        event = _determine_event(
            tool_results=[_qa_tool_result("qa_offer_declined")],
            is_fallback=False,
        )
        assert event == "project_completed"

    def test_qa_offer_returns_qa_offer(self):
        event = _determine_event(
            tool_results=[_qa_tool_result("qa_offer")],
            is_fallback=False,
        )
        assert event == "qa_offer"

    def test_qa_completed_no_issues_event(self):
        event = _determine_event(
            tool_results=[_qa_tool_result("qa_completed_no_issues")],
            is_fallback=False,
        )
        assert event == "qa_completed_no_issues"

    def test_qa_completed_with_findings_event(self):
        event = _determine_event(
            tool_results=[_qa_tool_result("qa_completed_with_findings")],
            is_fallback=False,
        )
        assert event == "qa_completed_with_findings"

    def test_remediation_confirmed_returns_qa_remediation_started(self):
        event = _determine_event(
            tool_results=[_qa_tool_result("remediation_confirmed")],
            is_fallback=False,
        )
        assert event == "qa_remediation_started"

    def test_remediation_declined_returns_qa_completed_no_issues(self):
        event = _determine_event(
            tool_results=[_qa_tool_result("remediation_declined")],
            is_fallback=False,
        )
        assert event == "qa_completed_no_issues"

    def test_project_completed_with_qa_offer_pending_returns_qa_offer(self):
        """When phase=completed and qa_offer_pending=True, event=qa_offer."""
        conv = self._conv_stub()
        conv.qa_offer_pending = True  # set by pre_loop after PROJECT_COMPLETED

        # No tool results, no system event needed — phase+flag is enough
        event = _determine_event(
            tool_results=[],
            is_fallback=False,
            conversation=conv,
        )
        assert event == "qa_offer"

    def test_project_completed_without_qa_offer_pending_returns_project_completed(self):
        conv = self._conv_stub()
        conv.qa_offer_pending = False

        event = _determine_event(
            tool_results=[],
            is_fallback=False,
            conversation=conv,
        )
        assert event == "project_completed"

    def test_completed_phase_with_pending_report_returns_qa_completed(self):
        conv = self._conv_stub(CONVERSATION_PHASE_COMPLETED)
        conv.pending_qa_report = '{"verdict": "failed"}'

        event = _determine_event(
            tool_results=[],
            is_fallback=False,
            conversation=conv,
        )
        assert event == "qa_completed_with_findings"


# ── Full integration: process_with_pre_transitions + QA paths ────────────────


class TestProcessWithPreTransitionsQA:
    def test_project_completed_eligible_returns_qa_offer_event(self, db_session: Session):
        project = _make_project(db_session, product_type="rest_api")
        conv = _make_conv(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        # Aria calls QA tool, QA tool makes offer, Aria responds
        call_count = {"n": 0}

        def _mock_llm(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _aria_step_call_qa()
            return _aria_step_respond("¿Quieres que ejecute el QA?")

        mock_tool_result = ToolResult(
            tool_name=ToolName.QA,
            data={
                "status": "qa_offer",
                "project_name": "Test Project",
                "completed_tasks": 5,
                "failed_tasks": 0,
            },
        )

        with (
            patch(
                "app.services.conversation.aria.orchestrator._call_aria_llm",
                side_effect=_mock_llm,
            ),
            patch(
                "app.services.conversation.aria.tools.qa_tool.QATool.execute",
                return_value=mock_tool_result,
            ),
        ):
            response = process_with_pre_transitions(
                db_session,
                conv.id,
                AriaInput(
                    source="system",
                    system_event=SystemEvent(
                        event_type=SystemEventType.PROJECT_COMPLETED,
                        data={"completed": 5, "failed": 0},
                    ),
                ),
            )

        assert response.event == "qa_offer"
        db_session.refresh(conv)
        assert conv.qa_offer_pending is True

    def test_qa_started_transitions_to_qa_running(self, db_session: Session):
        project = _make_project(db_session, product_type="rest_api")
        conv = _make_conv(
            db_session,
            project.id,
            phase=CONVERSATION_PHASE_COMPLETED,
            qa_offer_pending=True,
        )

        mock_session = MagicMock()
        mock_session.id = 99

        call_count = {"n": 0}

        def _mock_llm(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _aria_step_call_qa(hint="sí")
            return _aria_step_respond("Ejecutando QA…")

        mock_tool_result = ToolResult(
            tool_name=ToolName.QA,
            data={"status": "qa_started", "qa_session_id": 99},
        )

        with (
            patch(
                "app.services.conversation.aria.orchestrator._call_aria_llm",
                side_effect=_mock_llm,
            ),
            patch(
                "app.services.conversation.aria.tools.qa_tool.QATool.execute",
                return_value=mock_tool_result,
            ),
        ):
            response = process_with_pre_transitions(
                db_session,
                conv.id,
                AriaInput(source="user", user_message="sí"),
            )

        assert response.event == "qa_running"
        db_session.refresh(conv)
        assert conv.phase == CONVERSATION_PHASE_QA_RUNNING
        assert conv.qa_offer_pending is False

    def test_remediation_confirmed_transitions_to_executing(self, db_session: Session):
        project = _make_project(db_session, product_type="rest_api")
        report = json.dumps(
            {
                "verdict": "failed",
                "findings": [],
                "remediation_tasks": [],
                "findings_summary": {},
                "probes": [],
                "agents_called": [],
            }
        )
        conv = _make_conv(
            db_session,
            project.id,
            phase=CONVERSATION_PHASE_COMPLETED,
            pending_qa_report=report,
        )

        call_count = {"n": 0}

        def _mock_llm(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _aria_step_call_qa(hint="sí, créalas")
            return _aria_step_respond("Tareas creadas.")

        mock_tool_result = ToolResult(
            tool_name=ToolName.QA,
            data={"status": "remediation_confirmed", "tasks_created": 0},
        )

        with (
            patch(
                "app.services.conversation.aria.orchestrator._call_aria_llm",
                side_effect=_mock_llm,
            ),
            patch(
                "app.services.conversation.aria.tools.qa_tool.QATool.execute",
                return_value=mock_tool_result,
            ),
        ):
            response = process_with_pre_transitions(
                db_session,
                conv.id,
                AriaInput(source="user", user_message="sí, créalas"),
            )

        assert response.event == "qa_remediation_started"
        db_session.refresh(conv)
        assert conv.phase == CONVERSATION_PHASE_EXECUTING
        assert conv.pending_qa_report is None
