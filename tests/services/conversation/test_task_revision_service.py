"""Tests for task_revision_service."""

from sqlalchemy.orm import Session

from app.models.task import TASK_STATUS_COMPLETED, TASK_STATUS_PENDING, Task
from app.services.conversation.task_revision_service import (
    TaskRevision,
    apply_task_revisions,
)


def _make_atomic_task(db: Session, project_id: int, status: str = TASK_STATUS_PENDING) -> Task:
    task = Task(
        project_id=project_id,
        title="Test task",
        description="Original description",
        implementation_steps="Step 1\nStep 2",
        acceptance_criteria="Original criteria",
        technical_constraints="No constraints",
        planning_level="atomic",
        status=status,
    )
    db.add(task)
    db.flush()
    return task


def test_applies_revision_to_pending_task(db_session, make_project):
    project = make_project()
    task = _make_atomic_task(db_session, project.id)

    revisions = [
        TaskRevision(
            task_id=task.id,
            description="Updated description",
            implementation_steps="New step 1\nNew step 2",
        )
    ]

    result = apply_task_revisions(db_session, revisions)

    db_session.refresh(task)
    assert result.revised_count == 1
    assert result.skipped_ids == []
    assert task.description == "Updated description"
    assert task.implementation_steps == "New step 1\nNew step 2"
    assert task.acceptance_criteria == "Original criteria"  # untouched
    assert task.revised_at is not None


def test_skips_non_pending_task(db_session, make_project):
    project = make_project()
    task = _make_atomic_task(db_session, project.id, status=TASK_STATUS_COMPLETED)

    revisions = [TaskRevision(task_id=task.id, description="Should not apply")]

    result = apply_task_revisions(db_session, revisions)

    db_session.refresh(task)
    assert result.revised_count == 0
    assert task.id in result.skipped_ids
    assert task.description == "Original description"
    assert task.revised_at is None


def test_skips_nonexistent_task(db_session, make_project):
    result = apply_task_revisions(db_session, [TaskRevision(task_id=99999, description="ghost")])

    assert result.revised_count == 0
    assert 99999 in result.skipped_ids


def test_partial_revision_only_overwrites_provided_fields(db_session, make_project):
    project = make_project()
    task = _make_atomic_task(db_session, project.id)

    revisions = [TaskRevision(task_id=task.id, acceptance_criteria="New criteria only")]

    apply_task_revisions(db_session, revisions)
    db_session.refresh(task)

    assert task.acceptance_criteria == "New criteria only"
    assert task.description == "Original description"
    assert task.implementation_steps == "Step 1\nStep 2"


def test_empty_revisions_returns_zero(db_session):
    result = apply_task_revisions(db_session, [])

    assert result.revised_count == 0
    assert result.skipped_ids == []


def test_mixed_pending_and_non_pending(db_session, make_project):
    project = make_project()
    pending = _make_atomic_task(db_session, project.id, status=TASK_STATUS_PENDING)
    completed = _make_atomic_task(db_session, project.id, status=TASK_STATUS_COMPLETED)

    revisions = [
        TaskRevision(task_id=pending.id, description="New desc"),
        TaskRevision(task_id=completed.id, description="Should skip"),
    ]

    result = apply_task_revisions(db_session, revisions)

    assert result.revised_count == 1
    assert completed.id in result.skipped_ids
