"""Tests for ConfirmationTool."""

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_REVIEWING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.services.conversation.aria.contracts import ToolName
from app.services.conversation.aria.tools.confirmation_tool import ConfirmationTool
from app.services.conversation.confirmation_evaluator import ConfirmationEvaluatorResult


def _make_conv(db: Session, project_id: int, proposed_plan: str | None = None) -> Conversation:
    conv = Conversation(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=CONVERSATION_PHASE_REVIEWING,
        review_episode_attempts=1,
        requirements_ready=False,
        proposed_plan=proposed_plan,
    )
    db.add(conv)
    db.flush()
    return conv


class TestConfirmationTool:
    def test_name(self):
        assert ConfirmationTool().name == ToolName.CONFIRMATION

    def test_confirmed_true(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(db_session, project.id, proposed_plan="Use Google OAuth")

        mock_result = ConfirmationEvaluatorResult(
            confirmed=True, follow_up=None, reasoning="Clear yes"
        )

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            return_value=mock_result,
        ):
            result = ConfirmationTool().execute(db_session, project.id, conv, hint="Yes, go ahead")

        assert result.tool_name == ToolName.CONFIRMATION
        assert result.data["confirmed"] is True
        assert result.data["follow_up"] is None

    def test_confirmed_false_with_followup(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conv(db_session, project.id, proposed_plan="Use sessions")

        mock_result = ConfirmationEvaluatorResult(
            confirmed=False,
            follow_up="What kind of token expiry do you need?",
            reasoning="User wants to modify",
        )

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            return_value=mock_result,
        ):
            result = ConfirmationTool().execute(
                db_session, project.id, conv, hint="Actually I want JWT"
            )

        assert result.data["confirmed"] is False
        assert "token expiry" in result.data["follow_up"]

    def test_uses_proposed_plan_as_action_summary(self, db_session: Session, make_project):
        """The proposed_plan from conversation is passed as action_summary to the evaluator."""
        project = make_project()
        conv = _make_conv(db_session, project.id, proposed_plan="Implement Redis cache")

        captured = {}

        def _capture(inp):
            captured["action_summary"] = inp.action_summary
            return ConfirmationEvaluatorResult(confirmed=True, follow_up=None, reasoning="ok")

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            side_effect=_capture,
        ):
            ConfirmationTool().execute(db_session, project.id, conv, hint="Yes")

        assert captured["action_summary"] == "Implement Redis cache"

    def test_fallback_when_no_proposed_plan(self, db_session: Session, make_project):
        """Should not crash when proposed_plan is None."""
        project = make_project()
        conv = _make_conv(db_session, project.id, proposed_plan=None)

        mock_result = ConfirmationEvaluatorResult(confirmed=True, follow_up=None, reasoning="ok")

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            return_value=mock_result,
        ):
            result = ConfirmationTool().execute(db_session, project.id, conv, hint="ok")

        assert result.data["confirmed"] is True
