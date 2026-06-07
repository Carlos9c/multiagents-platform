"""Tests for ResumeProjectTool."""

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_PAUSED,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.models.task import PLANNING_LEVEL_ATOMIC, TASK_STATUS_COMPLETED, TASK_STATUS_PENDING, Task
from app.services.conversation.aria.contracts import ToolName
from app.services.conversation.aria.tools.resume_project_tool import ResumeProjectTool


def _make_paused_conv(
    db: Session, project_id: int, paused_reason: str | None = None
) -> Conversation:
    conv = Conversation(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=CONVERSATION_PHASE_PAUSED,
        review_episode_attempts=0,
        requirements_ready=False,
        paused_reason=paused_reason,
    )
    db.add(conv)
    db.flush()
    return conv


def _make_pending_task(db: Session, project_id: int) -> Task:
    task = Task(
        project_id=project_id,
        title="Pending task",
        description="desc",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_PENDING,
    )
    db.add(task)
    db.flush()
    return task


class TestResumeProjectTool:
    def test_name(self):
        assert ResumeProjectTool().name == ToolName.RESUME_PROJECT

    def test_resumed_when_pending_tasks_exist(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_paused_conv(db_session, project.id)
        _make_pending_task(db_session, project.id)
        db_session.commit()

        with patch(
            "app.services.conversation.aria.tools.resume_project_tool.clear_workflow_stop"
        ) as mock_clear:
            result = ResumeProjectTool().execute(db_session, project.id, conv)

        assert result.tool_name == ToolName.RESUME_PROJECT
        assert result.data["status"] == "resumed"
        assert result.data["project_id"] == project.id
        mock_clear.assert_called_once_with(project.id)

    def test_no_pending_tasks_returns_appropriate_status(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_paused_conv(db_session, project.id)
        # Add only a completed task — nothing pending
        completed = Task(
            project_id=project.id,
            title="Done task",
            description="d",
            planning_level=PLANNING_LEVEL_ATOMIC,
            status=TASK_STATUS_COMPLETED,
        )
        db_session.add(completed)
        db_session.commit()

        with patch(
            "app.services.conversation.aria.tools.resume_project_tool.clear_workflow_stop"
        ) as mock_clear:
            result = ResumeProjectTool().execute(db_session, project.id, conv)

        assert result.data["status"] == "no_pending_tasks"
        mock_clear.assert_not_called()

    def test_no_tasks_at_all_returns_no_pending(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_paused_conv(db_session, project.id)
        db_session.commit()

        with patch("app.services.conversation.aria.tools.resume_project_tool.clear_workflow_stop"):
            result = ResumeProjectTool().execute(db_session, project.id, conv)

        assert result.data["status"] == "no_pending_tasks"

    def test_clear_workflow_stop_called_with_correct_project_id(
        self, db_session: Session, make_project
    ):
        project = make_project()
        conv = _make_paused_conv(db_session, project.id)
        _make_pending_task(db_session, project.id)
        db_session.commit()

        with patch(
            "app.services.conversation.aria.tools.resume_project_tool.clear_workflow_stop"
        ) as mock_clear:
            ResumeProjectTool().execute(db_session, project.id, conv)

        mock_clear.assert_called_once_with(project.id)


# ── Orchestrator phase transitions for RESUME_PROJECT ─────────────────────────


class TestResumeProjectPhaseTransitions:
    def _make_conv_paused(
        self, db: Session, project_id: int, paused_reason: str | None = None
    ) -> Conversation:
        conv = Conversation(
            project_id=project_id,
            status=CONVERSATION_STATUS_ACTIVE,
            phase=CONVERSATION_PHASE_PAUSED,
            review_episode_attempts=0,
            requirements_ready=False,
            paused_reason=paused_reason,
        )
        db.add(conv)
        db.flush()
        return conv

    def test_resumed_status_transitions_phase_to_executing(self, db_session: Session, make_project):
        from app.models.conversation import CONVERSATION_PHASE_EXECUTING
        from app.services.conversation.aria.contracts import AriaStep, ToolResult
        from app.services.conversation.aria.orchestrator import _apply_phase_transitions

        project = make_project()
        conv = self._make_conv_paused(db_session, project.id, paused_reason="Docker failed")
        db_session.commit()

        aria_step = AriaStep(action="respond", response="Resuming.", reasoning="User asked.")
        tool_results = [
            ToolResult(
                tool_name=ToolName.RESUME_PROJECT,
                data={"status": "resumed", "project_id": project.id},
            )
        ]

        _apply_phase_transitions(db_session, conv, aria_step, tool_results)

        assert conv.phase == CONVERSATION_PHASE_EXECUTING
        assert conv.paused_reason is None

    def test_no_pending_tasks_status_does_not_change_phase(self, db_session: Session, make_project):
        from app.services.conversation.aria.contracts import AriaStep, ToolResult
        from app.services.conversation.aria.orchestrator import _apply_phase_transitions

        project = make_project()
        conv = self._make_conv_paused(db_session, project.id)
        db_session.commit()

        aria_step = AriaStep(action="respond", response="No tasks.", reasoning="Nothing to resume.")
        tool_results = [
            ToolResult(
                tool_name=ToolName.RESUME_PROJECT,
                data={"status": "no_pending_tasks"},
            )
        ]

        _apply_phase_transitions(db_session, conv, aria_step, tool_results)

        assert conv.phase == CONVERSATION_PHASE_PAUSED  # unchanged


# ── paused_reason storage on project-level abandonment ───────────────────────


class TestPausedReasonStorage:
    def _make_paused_conv_with_project_context(
        self, db: Session, project_id: int, failure_reason: str
    ) -> Conversation:
        from app.services.conversation.aria.contracts import (
            ProjectReviewContext,
            review_context_to_json,
        )

        ctx = ProjectReviewContext(failure_type="bootstrap", failure_reason=failure_reason)
        conv = Conversation(
            project_id=project_id,
            status=CONVERSATION_STATUS_ACTIVE,
            phase="reviewing",
            review_episode_attempts=1,
            requirements_ready=False,
            review_context=review_context_to_json(ctx),
        )
        db.add(conv)
        db.flush()
        return conv

    def test_paused_reason_stored_on_project_level_abandonment(
        self, db_session: Session, make_project
    ):
        from app.services.conversation.aria.orchestrator import _handle_review_abandoned

        project = make_project()
        conv = self._make_paused_conv_with_project_context(
            db_session, project.id, "Docker image not found"
        )
        db_session.commit()

        _handle_review_abandoned(
            db_session,
            conv,
            {"status": "abandoned", "is_project_level": True},
        )

        assert conv.paused_reason == "Docker image not found"
        assert conv.review_context is None

    def test_paused_reason_not_stored_on_task_level_abandonment(
        self, db_session: Session, make_project
    ):
        from app.services.conversation.aria.contracts import (
            TaskReviewContext,
            review_context_to_json,
        )
        from app.services.conversation.aria.orchestrator import _handle_review_abandoned

        project = make_project()
        ctx = TaskReviewContext(task_id=1, task_title="Setup DB")
        conv = Conversation(
            project_id=project.id,
            status=CONVERSATION_STATUS_ACTIVE,
            phase="reviewing",
            review_episode_attempts=1,
            requirements_ready=False,
            review_context=review_context_to_json(ctx),
        )
        db_session.add(conv)
        db_session.commit()

        _handle_review_abandoned(
            db_session,
            conv,
            {"status": "abandoned", "is_project_level": False},
        )

        assert conv.paused_reason is None
