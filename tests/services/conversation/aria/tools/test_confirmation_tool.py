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


# ── Episode history in ConfirmationTool ───────────────────────────────────────


class TestConfirmationToolEpisodeHistory:
    def _add_msg(self, db: Session, conv_id: int, role: str, content: str):
        from app.models.conversation import ConversationMessage

        msg = ConversationMessage(conversation_id=conv_id, role=role, content=content)
        db.add(msg)
        db.flush()
        return msg

    def _make_conv(self, db: Session, project_id: int) -> "Conversation":
        from app.models.conversation import (
            CONVERSATION_PHASE_REVIEWING,
            CONVERSATION_STATUS_ACTIVE,
            Conversation,
        )

        conv = Conversation(
            project_id=project_id,
            status=CONVERSATION_STATUS_ACTIVE,
            phase=CONVERSATION_PHASE_REVIEWING,
            review_episode_attempts=1,
            requirements_ready=False,
            proposed_plan="Use PostgreSQL with connection pooling",
        )
        db.add(conv)
        db.flush()
        return conv

    def _mock_result(self, confirmed: bool = True):
        return ConfirmationEvaluatorResult(confirmed=confirmed, follow_up=None, reasoning="ok")

    def test_episode_history_passed_to_evaluator_when_anchor_set(
        self, db_session: Session, make_project
    ):
        """With anchor set, episode_history contains only messages from anchor onwards."""
        project = make_project()
        conv = self._make_conv(db_session, project.id)

        self._add_msg(db_session, conv.id, "user", "Old message before review")
        anchor_msg = self._add_msg(db_session, conv.id, "system", "Task blocked: auth module")
        conv.review_episode_start_message_id = anchor_msg.id
        db_session.flush()

        self._add_msg(db_session, conv.id, "user", "Use Google OAuth")
        self._add_msg(db_session, conv.id, "assistant", "I will implement Google OAuth")

        captured: dict = {}

        def _capture(inp):
            captured["inp"] = inp
            return self._mock_result()

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            side_effect=_capture,
        ):
            ConfirmationTool().execute(db_session, project.id, conv, hint="yes")

        history = captured["inp"].episode_history
        contents = [t.content for t in history]
        assert "Old message before review" not in contents
        assert "Task blocked: auth module" in contents
        assert "Use Google OAuth" in contents

    def test_episode_history_empty_when_no_messages(self, db_session: Session, make_project):
        """No messages → episode_history is empty list, no crash."""
        project = make_project()
        conv = self._make_conv(db_session, project.id)
        db_session.flush()

        captured: dict = {}

        def _capture(inp):
            captured["inp"] = inp
            return self._mock_result()

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            side_effect=_capture,
        ):
            ConfirmationTool().execute(db_session, project.id, conv, hint="yes")

        assert captured["inp"].episode_history == []

    def test_fallback_scan_when_anchor_null(self, db_session: Session, make_project):
        """anchor=NULL uses legacy scan (last system message)."""
        project = make_project()
        conv = self._make_conv(db_session, project.id)
        assert conv.review_episode_start_message_id is None

        self._add_msg(db_session, conv.id, "user", "Pre-review message")
        self._add_msg(db_session, conv.id, "system", "Task blocked: deploy")
        self._add_msg(db_session, conv.id, "user", "Fix the dockerfile")

        captured: dict = {}

        def _capture(inp):
            captured["inp"] = inp
            return self._mock_result()

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            side_effect=_capture,
        ):
            ConfirmationTool().execute(db_session, project.id, conv, hint="yes")

        contents = [t.content for t in captured["inp"].episode_history]
        assert "Pre-review message" not in contents
        assert "Task blocked: deploy" in contents
        assert "Fix the dockerfile" in contents
