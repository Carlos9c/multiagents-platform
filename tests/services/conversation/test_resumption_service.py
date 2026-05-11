"""Tests for resumption_service."""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PENDING,
    TASK_STATUS_SUPERSEDED,
    Task,
)
from app.services.conversation.impact_assessment_agent import (
    ImpactAssessmentResult,
    NewTaskSpec,
    TaskRevisionSpec,
)
from app.services.conversation.resumption_service import (
    ResumptionError,
    resume_after_review,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_task(
    db: Session,
    project_id: int,
    *,
    status: str = TASK_STATUS_PENDING,
    sequence_order: int | None = None,
    planning_level: str = PLANNING_LEVEL_ATOMIC,
    parent_task_id: int | None = None,
    title: str = "A task",
    description: str = "Description",
) -> Task:
    task = Task(
        project_id=project_id,
        title=title,
        description=description,
        planning_level=planning_level,
        status=status,
        sequence_order=sequence_order,
        parent_task_id=parent_task_id,
    )
    db.add(task)
    db.flush()
    return task


def _narrow_assessment() -> ImpactAssessmentResult:
    return ImpactAssessmentResult(
        change_scope="narrow",
        reasoning="Only the blocked task is affected.",
        tasks_to_modify=[],
        tasks_to_add=[],
        resequence_needed=False,
    )


def _moderate_assessment(
    modify_task_id: int,
    add_after_task_id: int | None = None,
) -> ImpactAssessmentResult:
    return ImpactAssessmentResult(
        change_scope="moderate",
        reasoning="Some pending tasks need updating.",
        tasks_to_modify=[
            TaskRevisionSpec(
                task_id=modify_task_id,
                new_description="Updated description",
                new_objective=None,
                new_implementation_steps="New steps",
                new_acceptance_criteria=None,
                new_technical_constraints=None,
                reason="Affected by clarification",
            )
        ],
        tasks_to_add=[
            NewTaskSpec(
                insert_after_task_id=add_after_task_id,
                title="New injected task",
                description="Desc",
                objective="Obj",
                proposed_solution="Sol",
                implementation_steps="Steps",
                acceptance_criteria="Criteria",
                technical_constraints="None",
                reason="Gap identified",
            )
        ],
        resequence_needed=True,
    )


def _disruptive_assessment() -> ImpactAssessmentResult:
    return ImpactAssessmentResult(
        change_scope="disruptive",
        reasoning="High-level goals invalidated.",
        tasks_to_modify=[],
        tasks_to_add=[],
        resequence_needed=False,
    )


# ── Narrow tests ──────────────────────────────────────────────────────────────


def test_narrow_resets_blocked_task_to_pending(db_session, make_project):
    project = make_project()
    blocked = _make_task(db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=1)
    db_session.commit()

    with patch(
        "app.services.conversation.resumption_service.assess_impact",
        return_value=_narrow_assessment(),
    ):
        result = resume_after_review(
            db_session,
            project_id=project.id,
            blocked_task_id=blocked.id,
            user_clarification="Use port 8080 instead",
        )

    db_session.refresh(blocked)
    assert result.scope == "narrow"
    assert blocked.status == TASK_STATUS_PENDING
    assert blocked.review_attempts == 0
    assert "Use port 8080 instead" in (blocked.description or "")
    assert blocked.revised_at is not None


def test_narrow_raises_if_task_not_in_awaiting_review(db_session, make_project):
    project = make_project()
    task = _make_task(db_session, project.id, status=TASK_STATUS_PENDING)
    db_session.commit()

    with pytest.raises(ResumptionError, match="not in awaiting_review"):
        resume_after_review(
            db_session,
            project_id=project.id,
            blocked_task_id=task.id,
            user_clarification="irrelevant",
        )


# ── Moderate tests ────────────────────────────────────────────────────────────


def test_moderate_revises_pending_task_and_resets_blocked(db_session, make_project):
    project = make_project()
    blocked = _make_task(db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=2)
    pending = _make_task(db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=3)
    db_session.commit()

    with patch(
        "app.services.conversation.resumption_service.assess_impact",
        return_value=_moderate_assessment(modify_task_id=pending.id, add_after_task_id=pending.id),
    ):
        result = resume_after_review(
            db_session,
            project_id=project.id,
            blocked_task_id=blocked.id,
            user_clarification="Switch to PostgreSQL",
        )

    db_session.refresh(blocked)
    db_session.refresh(pending)

    assert result.scope == "moderate"
    assert blocked.status == TASK_STATUS_PENDING
    assert pending.description == "Updated description"
    assert pending.implementation_steps == "New steps"
    assert pending.revised_at is not None
    assert result.tasks_modified == 1
    assert result.tasks_added == 1


def test_moderate_inserts_new_task_in_correct_position(db_session, make_project):
    project = make_project()
    blocked = _make_task(db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=1)
    t2 = _make_task(db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=2)
    t3 = _make_task(db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=3)
    db_session.commit()

    assessment = ImpactAssessmentResult(
        change_scope="moderate",
        reasoning="Need new task after t2",
        tasks_to_modify=[],
        tasks_to_add=[
            NewTaskSpec(
                insert_after_task_id=t2.id,
                title="Injected",
                description="d",
                objective="o",
                proposed_solution="s",
                implementation_steps="i",
                acceptance_criteria="a",
                technical_constraints="c",
                reason="r",
            )
        ],
        resequence_needed=True,
    )

    with patch(
        "app.services.conversation.resumption_service.assess_impact",
        return_value=assessment,
    ):
        resume_after_review(
            db_session,
            project_id=project.id,
            blocked_task_id=blocked.id,
            user_clarification="Add config step",
        )

    db_session.refresh(t3)
    injected = (
        db_session.query(Task)
        .filter(Task.project_id == project.id, Task.title == "Injected")
        .first()
    )
    assert injected is not None
    # After resequence: blocked→1, t2→2, injected→3, t3→4
    assert injected.sequence_order is not None
    assert t3.sequence_order > injected.sequence_order


# ── Disruptive tests ──────────────────────────────────────────────────────────


def test_disruptive_supersedes_non_terminal_tasks(db_session, make_project):
    project = make_project()
    blocked = _make_task(db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=3)
    pending1 = _make_task(db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=4)
    pending2 = _make_task(db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=5)
    done = _make_task(db_session, project.id, status=TASK_STATUS_COMPLETED)
    db_session.commit()

    with patch(
        "app.services.conversation.resumption_service.assess_impact",
        return_value=_disruptive_assessment(),
    ), patch(
        "app.services.conversation.resumption_service.ProjectStartService"
    ) as mock_svc_class:
        mock_svc = MagicMock()
        mock_svc.start.return_value = MagicMock()
        mock_svc_class.return_value = mock_svc

        result = resume_after_review(
            db_session,
            project_id=project.id,
            blocked_task_id=blocked.id,
            user_clarification="Complete architecture change",
            updated_project_goal="New goal",
        )

    db_session.refresh(blocked)
    db_session.refresh(pending1)
    db_session.refresh(pending2)
    db_session.refresh(done)

    assert result.scope == "disruptive"
    assert blocked.status == TASK_STATUS_SUPERSEDED
    assert pending1.status == TASK_STATUS_SUPERSEDED
    assert pending2.status == TASK_STATUS_SUPERSEDED
    assert done.status == TASK_STATUS_COMPLETED  # untouched
    assert result.tasks_superseded == 3
    assert mock_svc.start.called


def test_disruptive_updates_project_description(db_session, make_project):
    project = make_project()
    blocked = _make_task(db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=1)
    db_session.commit()

    new_goal = "Completely revised project goal"

    with patch(
        "app.services.conversation.resumption_service.assess_impact",
        return_value=_disruptive_assessment(),
    ), patch("app.services.conversation.resumption_service.ProjectStartService") as mock_svc_class:
        mock_svc = MagicMock()
        mock_svc.start.return_value = MagicMock()
        mock_svc_class.return_value = mock_svc

        resume_after_review(
            db_session,
            project_id=project.id,
            blocked_task_id=blocked.id,
            user_clarification="Major pivot",
            updated_project_goal=new_goal,
        )

    db_session.refresh(project)
    assert project.description == new_goal
