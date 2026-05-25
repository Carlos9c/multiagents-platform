"""Tests for context_builder — ProjectSnapshot construction from DB."""

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_PHASE_REVIEWING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    Task,
)
from app.services.conversation.aria.context_builder import build_snapshot
from app.services.conversation.aria.contracts import (
    TaskReviewContext,
    review_context_to_json,
)


def _make_conversation(db: Session, project_id: int, **kwargs) -> Conversation:
    defaults = dict(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=CONVERSATION_PHASE_GATHERING,
        review_episode_attempts=0,
        requirements_ready=False,
    )
    defaults.update(kwargs)
    conv = Conversation(**defaults)
    db.add(conv)
    db.flush()
    return conv


def _make_task(db: Session, project_id: int, status: str, title: str = "T") -> Task:
    task = Task(
        project_id=project_id,
        title=title,
        description="desc",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=status,
        sequence_order=1,
    )
    db.add(task)
    db.flush()
    return task


class TestBuildSnapshot:
    def test_gathering_phase_empty_project(self, db_session: Session, make_project):
        project = make_project(name="My App")
        conv = _make_conversation(db_session, project.id)

        snapshot = build_snapshot(db_session, project.id, conv)

        assert snapshot.project_id == project.id
        assert snapshot.project_name == "My App"
        assert snapshot.phase == CONVERSATION_PHASE_GATHERING
        assert snapshot.requirements_ready is False
        assert snapshot.proposed_plan is None
        assert snapshot.review_context is None
        assert snapshot.task_counts.completed == 0
        assert snapshot.task_counts.pending == 0

    def test_task_counts_populated(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        _make_task(db_session, project.id, TASK_STATUS_COMPLETED, "Done task")
        _make_task(db_session, project.id, TASK_STATUS_FAILED, "Failed task")
        _make_task(db_session, project.id, TASK_STATUS_PENDING, "Pending task")
        _make_task(db_session, project.id, TASK_STATUS_AWAITING_REVIEW, "Blocked task")

        snapshot = build_snapshot(db_session, project.id, conv)

        assert snapshot.task_counts.completed == 1
        assert snapshot.task_counts.failed == 1
        assert snapshot.task_counts.pending == 1
        assert snapshot.task_counts.blocked == 1

    def test_review_context_deserialised(self, db_session: Session, make_project):
        project = make_project()
        ctx = TaskReviewContext(
            task_id=99,
            task_title="Build API",
            validation_notes="Port conflict",
        )
        conv = _make_conversation(
            db_session,
            project.id,
            phase=CONVERSATION_PHASE_REVIEWING,
            review_context=review_context_to_json(ctx),
        )

        snapshot = build_snapshot(db_session, project.id, conv)

        assert snapshot.review_context is not None
        assert snapshot.review_context.kind == "task"
        assert snapshot.review_context.task_id == 99

    def test_requirements_ready_flag(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(
            db_session, project.id, requirements_ready=True, requirements_draft="Build a CLI tool"
        )

        snapshot = build_snapshot(db_session, project.id, conv)

        assert snapshot.requirements_ready is True
        assert snapshot.requirements_draft == "Build a CLI tool"

    def test_proposed_plan_included(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(
            db_session,
            project.id,
            phase=CONVERSATION_PHASE_REVIEWING,
            proposed_plan="Use asyncio instead of threads",
        )

        snapshot = build_snapshot(db_session, project.id, conv)

        assert snapshot.proposed_plan == "Use asyncio instead of threads"

    def test_format_for_prompt_no_error(self, db_session: Session, make_project):
        project = make_project(name="Test", description="A test project")
        _make_task(db_session, project.id, TASK_STATUS_COMPLETED)
        _make_task(db_session, project.id, TASK_STATUS_PENDING)
        conv = _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        snapshot = build_snapshot(db_session, project.id, conv)
        prompt_text = snapshot.format_for_prompt()

        assert "Test" in prompt_text
        assert "executing" in prompt_text
        assert "1 completadas" in prompt_text
        assert "1 pendientes" in prompt_text

    def test_invalid_review_context_json_handled_gracefully(
        self, db_session: Session, make_project
    ):
        project = make_project()
        conv = _make_conversation(
            db_session,
            project.id,
            phase=CONVERSATION_PHASE_REVIEWING,
            review_context="not valid json {{{",
        )

        # Should not raise — returns None for review_context
        snapshot = build_snapshot(db_session, project.id, conv)
        assert snapshot.review_context is None
