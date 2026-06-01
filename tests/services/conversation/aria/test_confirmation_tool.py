"""Tests for ConfirmationTool — Solution C: hint fallback to last user message."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_REVIEWING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
    ConversationMessage,
)
from app.services.conversation.aria.contracts import ToolName
from app.services.conversation.aria.tools.confirmation_tool import ConfirmationTool


def _make_conversation(db: Session, project_id: int, **kwargs) -> Conversation:
    defaults = dict(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=CONVERSATION_PHASE_REVIEWING,
        review_episode_attempts=0,
        requirements_ready=False,
        proposed_plan="Run the validator script without shell operators",
    )
    defaults.update(kwargs)
    conv = Conversation(**defaults)
    db.add(conv)
    db.flush()
    return conv


def _add_message(db: Session, conversation_id: int, role: str, content: str) -> None:
    msg = ConversationMessage(conversation_id=conversation_id, role=role, content=content)
    db.add(msg)
    db.flush()


class TestConfirmationToolHintFallback:
    """ConfirmationTool must always pass real user text to evaluate_confirmation."""

    def test_uses_hint_when_provided(self, db_session: Session, make_project):
        """Explicit hint takes precedence over DB fallback."""
        project = make_project()
        conv = _make_conversation(db_session, project.id)
        _add_message(db_session, conv.id, "user", "Some earlier message")

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            return_value=MagicMock(confirmed=True, follow_up=None, reasoning="ok"),
        ) as mock_eval:
            tool = ConfirmationTool()
            tool.execute(
                db=db_session, project_id=project.id, conversation=conv, hint="Sí, adelante"
            )

        call_args = mock_eval.call_args[0][0]
        assert call_args.user_response == "Sí, adelante"

    def test_falls_back_to_last_user_message_when_hint_is_none(
        self, db_session: Session, make_project
    ):
        """When hint is None, ConfirmationTool reads the most recent user message."""
        project = make_project()
        conv = _make_conversation(db_session, project.id)
        _add_message(db_session, conv.id, "assistant", "Here is my plan. Do you confirm?")
        _add_message(db_session, conv.id, "user", "Valida usando python sin operadores de shell")

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            return_value=MagicMock(confirmed=True, follow_up=None, reasoning="ok"),
        ) as mock_eval:
            tool = ConfirmationTool()
            tool.execute(db=db_session, project_id=project.id, conversation=conv, hint=None)

        call_args = mock_eval.call_args[0][0]
        assert call_args.user_response == "Valida usando python sin operadores de shell"

    def test_falls_back_empty_string_when_no_user_messages_and_no_hint(
        self, db_session: Session, make_project
    ):
        """No user messages + no hint → empty string (not a crash)."""
        project = make_project()
        conv = _make_conversation(db_session, project.id)
        # Only assistant messages, no user messages
        _add_message(db_session, conv.id, "assistant", "Here is my plan.")

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            return_value=MagicMock(confirmed=False, follow_up="Please clarify.", reasoning="ok"),
        ) as mock_eval:
            tool = ConfirmationTool()
            tool.execute(db=db_session, project_id=project.id, conversation=conv, hint=None)

        call_args = mock_eval.call_args[0][0]
        assert call_args.user_response == ""

    def test_passes_proposed_plan_to_evaluator(self, db_session: Session, make_project):
        """The proposed_plan stored in conversation is always passed as action_summary."""
        project = make_project()
        plan = "Run python -m pytest tests/test_schemas.py"
        conv = _make_conversation(db_session, project.id, proposed_plan=plan)

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            return_value=MagicMock(confirmed=True, follow_up=None, reasoning="ok"),
        ) as mock_eval:
            tool = ConfirmationTool()
            tool.execute(db=db_session, project_id=project.id, conversation=conv, hint="yes")

        call_args = mock_eval.call_args[0][0]
        assert call_args.action_summary == plan

    def test_result_data_contains_confirmed_field(self, db_session: Session, make_project):
        """ToolResult.data always includes the 'confirmed' key."""
        project = make_project()
        conv = _make_conversation(db_session, project.id)

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            return_value=MagicMock(confirmed=True, follow_up=None, reasoning="confirmed"),
        ):
            tool = ConfirmationTool()
            result = tool.execute(
                db=db_session, project_id=project.id, conversation=conv, hint="go ahead"
            )

        assert result.tool_name == ToolName.CONFIRMATION
        assert result.data["confirmed"] is True

    def test_fallback_picks_most_recent_user_message(self, db_session: Session, make_project):
        """Fallback should use the LAST user message, not an earlier one."""
        project = make_project()
        conv = _make_conversation(db_session, project.id)
        _add_message(db_session, conv.id, "user", "First question answer")
        _add_message(db_session, conv.id, "assistant", "Got it. Here's my plan. Confirm?")
        _add_message(db_session, conv.id, "user", "Yes, proceed with that approach")

        with patch(
            "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
            return_value=MagicMock(confirmed=True, follow_up=None, reasoning="ok"),
        ) as mock_eval:
            tool = ConfirmationTool()
            tool.execute(db=db_session, project_id=project.id, conversation=conv, hint=None)

        call_args = mock_eval.call_args[0][0]
        assert call_args.user_response == "Yes, proceed with that approach"
