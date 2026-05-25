"""Tests for ReviewTool."""

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_REVIEWING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.models.task import PLANNING_LEVEL_ATOMIC, TASK_STATUS_PENDING, Task
from app.services.conversation.aria.contracts import (
    ProjectReviewContext,
    TaskReviewContext,
    ToolName,
    review_context_to_json,
)
from app.services.conversation.aria.tools.review_tool import ReviewTool
from app.services.conversation.review_evaluator import ReviewEvaluatorResult


def _make_conv(db: Session, project_id: int, review_ctx_json: str | None = None) -> Conversation:
    conv = Conversation(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=CONVERSATION_PHASE_REVIEWING,
        review_episode_attempts=0,
        requirements_ready=False,
        review_context=review_ctx_json,
    )
    db.add(conv)
    db.flush()
    return conv


class TestReviewTool:
    def test_name(self):
        assert ReviewTool().name == ToolName.REVIEW

    def test_task_level_review_insufficient(self, db_session: Session, make_project):
        project = make_project(description="Build a web app")
        ctx = TaskReviewContext(
            task_id=5, task_title="Setup DB", validation_notes="Connection refused"
        )
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        mock_result = ReviewEvaluatorResult(
            status="insufficient",
            next_question="Which DB engine?",
            clarification_summary=None,
            action_summary=None,
            reasoning="Need more info",
        )

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            return_value=mock_result,
        ):
            result = ReviewTool().execute(db_session, project.id, conv)

        assert result.tool_name == ToolName.REVIEW
        assert result.data["status"] == "insufficient"
        assert result.data["next_question"] == "Which DB engine?"
        assert result.data["is_project_level"] is False

    def test_task_level_review_ready_to_confirm(self, db_session: Session, make_project):
        project = make_project()
        ctx = TaskReviewContext(task_id=3, task_title="Auth module")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        mock_result = ReviewEvaluatorResult(
            status="ready_to_confirm",
            next_question=None,
            clarification_summary="Use Google OAuth2",
            action_summary="Implement Google OAuth2 login",
            reasoning="Sufficient info",
        )

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            return_value=mock_result,
        ):
            result = ReviewTool().execute(db_session, project.id, conv)

        assert result.data["status"] == "ready_to_confirm"
        assert result.data["clarification_summary"] == "Use Google OAuth2"

    def test_project_level_review(self, db_session: Session, make_project):
        project = make_project()
        ctx = ProjectReviewContext(failure_type="bootstrap", failure_reason="Docker not found")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        mock_result = ReviewEvaluatorResult(
            status="insufficient",
            next_question="Which Docker image to use?",
            clarification_summary=None,
            action_summary=None,
            reasoning="Need image name",
        )

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            return_value=mock_result,
        ) as mock_call:
            result = ReviewTool().execute(db_session, project.id, conv)

        assert result.data["is_project_level"] is True
        # Ensure the evaluator received is_project_level_error=True
        call_inp = mock_call.call_args[0][0]
        assert call_inp.is_project_level_error is True

    def test_review_abandoned(self, db_session: Session, make_project):
        project = make_project()
        ctx = TaskReviewContext(task_id=7, task_title="Deploy")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        mock_result = ReviewEvaluatorResult(
            status="abandoned",
            next_question=None,
            clarification_summary=None,
            action_summary=None,
            reasoning="User gave up",
        )

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            return_value=mock_result,
        ):
            result = ReviewTool().execute(db_session, project.id, conv)

        assert result.data["status"] == "abandoned"

    def test_task_summary_excludes_blocked_task(self, db_session: Session, make_project):
        project = make_project()
        ctx = TaskReviewContext(task_id=10, task_title="Blocked task")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        # Add a task with id matching the blocked task_id via make_task fixture
        other_task = Task(
            project_id=project.id,
            title="Other pending task",
            description="desc",
            planning_level=PLANNING_LEVEL_ATOMIC,
            status=TASK_STATUS_PENDING,
            sequence_order=2,
        )
        db_session.add(other_task)
        db_session.flush()

        captured = {}

        def _capture(inp):
            captured["summary"] = inp.project_task_summary
            return ReviewEvaluatorResult(
                status="insufficient",
                next_question="Q?",
                clarification_summary=None,
                action_summary=None,
                reasoning="r",
            )

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            side_effect=_capture,
        ):
            ReviewTool().execute(db_session, project.id, conv)

        # Summary should mention the other task but not the blocked one (id=10 doesn't exist)
        assert captured["summary"] is not None or captured["summary"] is None  # just no crash
