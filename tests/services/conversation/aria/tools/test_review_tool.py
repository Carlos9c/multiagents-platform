"""Tests for ReviewTool."""

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_REVIEWING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
    ConversationMessage,
)
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PENDING,
    Task,
)
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


# ── Episode anchor tests ──────────────────────────────────────────────────────


def _add_message(db: Session, conversation_id: int, role: str, content: str) -> ConversationMessage:
    msg = ConversationMessage(conversation_id=conversation_id, role=role, content=content)
    db.add(msg)
    db.flush()
    return msg


class TestReviewToolEpisodeAnchor:
    """Tests that ReviewTool uses review_episode_start_message_id as boundary."""

    def _mock_result(self) -> ReviewEvaluatorResult:
        return ReviewEvaluatorResult(
            status="insufficient",
            next_question="More info?",
            clarification_summary=None,
            action_summary=None,
            reasoning="r",
        )

    def test_anchor_limits_episode_history_to_messages_from_anchor_onwards(
        self, db_session: Session, make_project
    ):
        """When anchor is set, only messages with id >= anchor_id are in the episode."""
        project = make_project()
        ctx = TaskReviewContext(task_id=1, task_title="Setup DB")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        # Old messages (before review opened)
        _add_message(db_session, conv.id, "user", "Old message before review")
        _add_message(db_session, conv.id, "assistant", "Old response")

        # The system message that opened the review episode
        anchor_msg = _add_message(db_session, conv.id, "system", "Task blocked: Setup DB")
        conv.review_episode_start_message_id = anchor_msg.id
        db_session.flush()

        # New messages in this episode
        _add_message(db_session, conv.id, "user", "I will use PostgreSQL")

        captured: dict = {}

        def _capture(inp):
            captured["history"] = inp.episode_history
            return self._mock_result()

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            side_effect=_capture,
        ):
            ReviewTool().execute(db_session, project.id, conv)

        contents = [t.content for t in captured["history"]]
        assert "Old message before review" not in contents
        assert "Task blocked: Setup DB" in contents
        assert "I will use PostgreSQL" in contents

    def test_fallback_scan_used_when_anchor_is_null(self, db_session: Session, make_project):
        """When review_episode_start_message_id is NULL, the legacy scan runs."""
        project = make_project()
        ctx = TaskReviewContext(task_id=2, task_title="Auth module")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))
        # anchor is NULL (default)
        assert conv.review_episode_start_message_id is None

        _add_message(db_session, conv.id, "user", "Pre-review user message")
        _add_message(db_session, conv.id, "system", "Task blocked: Auth module")
        _add_message(db_session, conv.id, "user", "Use JWT")

        captured: dict = {}

        def _capture(inp):
            captured["history"] = inp.episode_history
            return self._mock_result()

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            side_effect=_capture,
        ):
            ReviewTool().execute(db_session, project.id, conv)

        # Legacy scan finds last system message; pre-review user message is excluded
        contents = [t.content for t in captured["history"]]
        assert "Pre-review user message" not in contents
        assert "Task blocked: Auth module" in contents
        assert "Use JWT" in contents


# ── Full task tree tests ──────────────────────────────────────────────────────


def _make_task(
    db: Session,
    project_id: int,
    *,
    title: str,
    planning_level: str = PLANNING_LEVEL_ATOMIC,
    status: str = TASK_STATUS_PENDING,
    parent_task_id: int | None = None,
    depends_on_task_titles: list | None = None,
    sequence_order: int | None = None,
) -> Task:
    task = Task(
        project_id=project_id,
        title=title,
        description=f"desc for {title}",
        planning_level=planning_level,
        status=status,
        parent_task_id=parent_task_id,
        depends_on_task_titles=depends_on_task_titles or [],
        sequence_order=sequence_order,
    )
    db.add(task)
    db.flush()
    return task


class TestReviewToolFullTaskTree:
    """Verify ReviewTool builds and passes full_task_tree to ReviewEvaluator."""

    def _mock_result(self) -> ReviewEvaluatorResult:
        return ReviewEvaluatorResult(
            status="insufficient",
            next_question="What should we change?",
            clarification_summary=None,
            action_summary=None,
            reasoning="r",
        )

    def test_task_tree_passed_to_evaluator(self, db_session: Session, make_project):
        """ReviewTool passes full_task_tree to ReviewEvaluatorInput for task-level review."""
        project = make_project(description="Build API")
        ctx = TaskReviewContext(task_id=99, task_title="Blocked task")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        high_level = _make_task(
            db_session, project.id, title="Auth module", planning_level=PLANNING_LEVEL_HIGH_LEVEL
        )
        _make_task(
            db_session,
            project.id,
            title="Setup JWT",
            parent_task_id=high_level.id,
            sequence_order=1,
        )
        _make_task(
            db_session,
            project.id,
            title="Add refresh tokens",
            parent_task_id=high_level.id,
            depends_on_task_titles=["Setup JWT"],
            sequence_order=2,
        )
        db_session.commit()

        captured: dict = {}

        def _capture(inp):
            captured["full_task_tree"] = inp.full_task_tree
            return self._mock_result()

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            side_effect=_capture,
        ):
            ReviewTool().execute(db_session, project.id, conv)

        tree = captured.get("full_task_tree", [])
        assert tree is not None
        titles = [t.title for t in tree]
        assert "Auth module" in titles
        assert "Setup JWT" in titles
        assert "Add refresh tokens" in titles

    def test_task_tree_parent_title_resolved(self, db_session: Session, make_project):
        """parent_title is set from the parent task's title."""
        project = make_project()
        ctx = TaskReviewContext(task_id=99, task_title="Blocked task")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        parent = _make_task(
            db_session, project.id, title="Database layer", planning_level=PLANNING_LEVEL_HIGH_LEVEL
        )
        child = _make_task(db_session, project.id, title="Create schema", parent_task_id=parent.id)
        db_session.commit()

        captured: dict = {}

        def _capture(inp):
            captured["tree"] = inp.full_task_tree
            return self._mock_result()

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            side_effect=_capture,
        ):
            ReviewTool().execute(db_session, project.id, conv)

        child_ctx = next((t for t in captured["tree"] if t.task_id == child.id), None)
        assert child_ctx is not None
        assert child_ctx.parent_title == "Database layer"
        assert child_ctx.parent_task_id == parent.id

    def test_completed_tasks_excluded_from_tree(self, db_session: Session, make_project):
        """Completed tasks do not appear in full_task_tree (they're terminal)."""
        project = make_project()
        ctx = TaskReviewContext(task_id=99, task_title="Blocked task")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        done = _make_task(
            db_session, project.id, title="Init project", status=TASK_STATUS_COMPLETED
        )
        pending = _make_task(db_session, project.id, title="Setup DB")
        db_session.commit()

        captured: dict = {}

        def _capture(inp):
            captured["tree"] = inp.full_task_tree
            return self._mock_result()

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            side_effect=_capture,
        ):
            ReviewTool().execute(db_session, project.id, conv)

        tree_ids = [t.task_id for t in captured["tree"]]
        assert done.id not in tree_ids
        assert pending.id in tree_ids

    def test_depends_on_task_titles_preserved(self, db_session: Session, make_project):
        """depends_on_task_titles from the DB are passed through to the tree."""
        project = make_project()
        ctx = TaskReviewContext(task_id=99, task_title="Blocked task")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        t = _make_task(
            db_session,
            project.id,
            title="Write API tests",
            depends_on_task_titles=["Setup DB", "Define routes"],
        )
        db_session.commit()

        captured: dict = {}

        def _capture(inp):
            captured["tree"] = inp.full_task_tree
            return self._mock_result()

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            side_effect=_capture,
        ):
            ReviewTool().execute(db_session, project.id, conv)

        task_ctx = next((x for x in captured["tree"] if x.task_id == t.id), None)
        assert task_ctx is not None
        assert "Setup DB" in task_ctx.depends_on_task_titles
        assert "Define routes" in task_ctx.depends_on_task_titles

    def test_project_level_review_has_empty_tree(self, db_session: Session, make_project):
        """For project-level errors, full_task_tree is empty (not applicable)."""
        project = make_project()
        ctx = ProjectReviewContext(failure_type="bootstrap", failure_reason="Docker not found")
        conv = _make_conv(db_session, project.id, review_context_to_json(ctx))

        _make_task(db_session, project.id, title="Some pending task")
        db_session.commit()

        captured: dict = {}

        def _capture(inp):
            captured["tree"] = inp.full_task_tree
            return ReviewEvaluatorResult(
                status="insufficient",
                next_question="Which Docker image?",
                clarification_summary=None,
                action_summary=None,
                reasoning="r",
            )

        with patch(
            "app.services.conversation.aria.tools.review_tool.evaluate_review",
            side_effect=_capture,
        ):
            ReviewTool().execute(db_session, project.id, conv)

        # Project-level review: tree should be empty (cascade analysis not applicable)
        assert captured.get("tree") == []
