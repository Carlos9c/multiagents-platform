"""Tests for RequirementsTool."""

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
    ConversationMessage,
)
from app.services.conversation.aria.contracts import ToolName
from app.services.conversation.aria.tools.requirements_tool import RequirementsTool
from app.services.conversation.requirements_evaluator import RequirementsEvaluatorResult


def _make_conv(db: Session, project_id: int, draft: str | None = None) -> Conversation:
    conv = Conversation(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=CONVERSATION_PHASE_GATHERING,
        review_episode_attempts=0,
        requirements_ready=False,
        requirements_draft=draft,
    )
    db.add(conv)
    db.flush()
    return conv


def _add_message(db: Session, conv_id: int, role: str, content: str) -> None:
    msg = ConversationMessage(conversation_id=conv_id, role=role, content=content)
    db.add(msg)
    db.flush()


class TestRequirementsTool:
    def test_name(self):
        assert RequirementsTool().name == ToolName.REQUIREMENTS

    def test_returns_needs_more(self, db_session: Session, make_project):
        project = make_project(name="My API")
        conv = _make_conv(db_session, project.id)
        _add_message(db_session, conv.id, "user", "I want a REST API")

        mock_result = RequirementsEvaluatorResult(
            status="needs_more",
            next_question="What language/framework?",
            updated_draft="REST API project",
            reasoning="Need more info",
        )

        with patch(
            "app.services.conversation.aria.tools.requirements_tool.evaluate_requirements",
            return_value=mock_result,
        ):
            result = RequirementsTool().execute(db_session, project.id, conv)

        assert result.tool_name == ToolName.REQUIREMENTS
        assert result.data["status"] == "needs_more"
        assert result.data["next_question"] == "What language/framework?"
        assert result.data["updated_draft"] == "REST API project"

    def test_returns_sufficient(self, db_session: Session, make_project):
        project = make_project(name="My API")
        conv = _make_conv(db_session, project.id, draft="Build a FastAPI service")
        _add_message(db_session, conv.id, "user", "FastAPI, Python, PostgreSQL")

        mock_result = RequirementsEvaluatorResult(
            status="sufficient",
            next_question=None,
            updated_draft="FastAPI service with PostgreSQL",
            reasoning="Has stack details",
        )

        with patch(
            "app.services.conversation.aria.tools.requirements_tool.evaluate_requirements",
            return_value=mock_result,
        ):
            result = RequirementsTool().execute(db_session, project.id, conv)

        assert result.data["status"] == "sufficient"
        assert result.data["next_question"] is None

    def test_history_excludes_system_messages(self, db_session: Session, make_project):
        """System messages should not be included in the history passed to the evaluator."""
        project = make_project()
        conv = _make_conv(db_session, project.id)
        _add_message(db_session, conv.id, "user", "I want an app")
        _add_message(db_session, conv.id, "system", "[execution_started] {}")
        _add_message(db_session, conv.id, "assistant", "Tell me more")

        captured_input = {}

        def _capture(inp):
            captured_input["history"] = inp.history
            return RequirementsEvaluatorResult(
                status="needs_more",
                next_question="Q?",
                updated_draft=None,
                reasoning="r",
            )

        with patch(
            "app.services.conversation.aria.tools.requirements_tool.evaluate_requirements",
            side_effect=_capture,
        ):
            RequirementsTool().execute(db_session, project.id, conv)

        roles = [t.role for t in captured_input["history"]]
        assert "system" not in roles
        assert "user" in roles
        assert "assistant" in roles
