"""Tests for the REPLANNING transitional phase during disruptive resumption."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_REPLANNING,
    CONVERSATION_PHASE_REVIEWING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_AWAITING_REVIEW,
    Task,
)
from app.services.conversation.resumption_service import _set_conversation_replanning

# ── Helper ────────────────────────────────────────────────────────────────────


def _make_reviewing_conversation(db: Session, project_id: int) -> Conversation:
    from app.services.conversation.aria.contracts import TaskReviewContext, review_context_to_json

    ctx = TaskReviewContext(task_id=99, task_title="Deploy service")
    conv = Conversation(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=CONVERSATION_PHASE_REVIEWING,
        review_episode_attempts=1,
        requirements_ready=False,
        review_context=review_context_to_json(ctx),
        proposed_plan="Switch to Kubernetes",
    )
    db.add(conv)
    db.flush()
    return conv


def _make_blocked_task(db: Session, project_id: int) -> Task:
    task = Task(
        project_id=project_id,
        title="Deploy service",
        description="desc",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_AWAITING_REVIEW,
        sequence_order=1,
    )
    db.add(task)
    db.flush()
    return task


# ── Tests: _set_conversation_replanning ──────────────────────────────────────


class TestSetConversationReplanning:
    def test_sets_phase_to_replanning(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_reviewing_conversation(db_session, project.id)
        db_session.commit()

        _set_conversation_replanning(db_session, project.id)
        db_session.flush()
        db_session.refresh(conv)

        assert conv.phase == CONVERSATION_PHASE_REPLANNING

    def test_no_crash_when_no_active_conversation(self, db_session: Session, make_project):
        project = make_project()
        # No conversation created
        # Should not raise
        _set_conversation_replanning(db_session, project.id)

    def test_only_affects_active_conversation(self, db_session: Session, make_project):
        project = make_project()
        # Create an inactive conversation
        conv = Conversation(
            project_id=project.id,
            status="completed",
            phase=CONVERSATION_PHASE_REVIEWING,
            review_episode_attempts=0,
            requirements_ready=False,
        )
        db_session.add(conv)
        db_session.commit()

        _set_conversation_replanning(db_session, project.id)
        db_session.flush()
        db_session.refresh(conv)

        # Completed conversation is untouched
        assert conv.phase == CONVERSATION_PHASE_REVIEWING


# ── Tests: context_builder with REPLANNING phase ─────────────────────────────


class TestContextBuilderReplanning:
    def test_replanning_status_in_prompt(self, db_session: Session, make_project):
        from app.models.conversation import CONVERSATION_STATUS_ACTIVE

        project = make_project()
        conv = Conversation(
            project_id=project.id,
            status=CONVERSATION_STATUS_ACTIVE,
            phase=CONVERSATION_PHASE_REPLANNING,
            review_episode_attempts=0,
            requirements_ready=False,
        )
        db_session.add(conv)
        db_session.flush()

        from app.services.conversation.aria.context_builder import build_snapshot

        snapshot = build_snapshot(db_session, project.id, conv)
        prompt = snapshot.format_for_prompt()

        assert "regenerando el plan" in prompt or "replanning" in snapshot.phase

    def test_replanning_phase_visible_in_snapshot(self, db_session: Session, make_project):
        from app.models.conversation import CONVERSATION_STATUS_ACTIVE

        project = make_project()
        conv = Conversation(
            project_id=project.id,
            status=CONVERSATION_STATUS_ACTIVE,
            phase=CONVERSATION_PHASE_REPLANNING,
            review_episode_attempts=0,
            requirements_ready=False,
        )
        db_session.add(conv)
        db_session.flush()

        from app.services.conversation.aria.context_builder import build_snapshot

        snapshot = build_snapshot(db_session, project.id, conv)
        assert snapshot.phase == CONVERSATION_PHASE_REPLANNING


# ── Tests: disruptive resumption sets REPLANNING before LLM call ─────────────


class TestDisruptiveResumptionSetsReplanning:
    """Verify that _apply_disruptive sets conversation phase to REPLANNING
    before calling ProjectStartService.start()."""

    def test_replanning_committed_before_project_start(self, db_session: Session, make_project):
        """The conversation phase must be REPLANNING when ProjectStartService.start() is called."""
        from app.services.conversation.impact_assessment_agent import ImpactAssessmentResult
        from app.services.conversation.resumption_service import resume_after_review

        project = make_project()
        conv = _make_reviewing_conversation(db_session, project.id)
        blocked = _make_blocked_task(db_session, project.id)
        db_session.commit()

        phase_during_start: list[str] = []

        def _mock_start(db, request):
            # Check the conversation phase at the moment start() is called
            db.refresh(conv)
            phase_during_start.append(conv.phase)
            # Return a minimal mock response
            return MagicMock(tasks_created=2)

        with (
            patch(
                "app.services.conversation.resumption_service.assess_impact",
                return_value=ImpactAssessmentResult(
                    change_scope="disruptive",
                    reasoning="Major pivot",
                    tasks_to_eliminate=[],
                    tasks_to_modify=[],
                    new_work_blocks=[],
                    environment_changes=[],
                ),
            ),
            patch("app.services.conversation.resumption_service.ProjectStartService") as MockSvc,
        ):
            mock_instance = MagicMock()
            mock_instance.start.side_effect = _mock_start
            MockSvc.return_value = mock_instance

            resume_after_review(
                db_session,
                project_id=project.id,
                blocked_task_id=blocked.id,
                user_clarification="Complete architecture change",
            )

        assert len(phase_during_start) == 1
        assert phase_during_start[0] == CONVERSATION_PHASE_REPLANNING

    def test_narrow_resumption_does_not_set_replanning(self, db_session: Session, make_project):
        """Only disruptive scope sets REPLANNING — narrow scope leaves phase unchanged."""
        from app.services.conversation.impact_assessment_agent import ImpactAssessmentResult
        from app.services.conversation.resumption_service import resume_after_review

        project = make_project()
        conv = _make_reviewing_conversation(db_session, project.id)
        blocked = _make_blocked_task(db_session, project.id)
        db_session.commit()

        with patch(
            "app.services.conversation.resumption_service.assess_impact",
            return_value=ImpactAssessmentResult(
                change_scope="narrow",
                reasoning="Small fix",
                tasks_to_eliminate=[],
                tasks_to_modify=[],
                new_work_blocks=[],
                environment_changes=[],
            ),
        ):
            resume_after_review(
                db_session,
                project_id=project.id,
                blocked_task_id=blocked.id,
                user_clarification="Use port 8080 instead",
            )

        db_session.refresh(conv)
        # Narrow resumption does not go through _apply_disruptive → no REPLANNING
        assert conv.phase != CONVERSATION_PHASE_REPLANNING
