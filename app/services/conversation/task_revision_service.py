"""Service for revising pending atomic tasks in-place during moderate-change resumptions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.task import TASK_STATUS_PENDING, Task

logger = logging.getLogger(__name__)


@dataclass
class TaskRevision:
    task_id: int
    description: str | None = None
    objective: str | None = None
    proposed_solution: str | None = None
    implementation_steps: str | None = None
    acceptance_criteria: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None
    depends_on_task_titles: list[str] | None = None


@dataclass
class TaskRevisionResult:
    revised_count: int
    skipped_ids: list[int]


class TaskRevisionError(Exception):
    pass


def apply_task_revisions(
    db: Session,
    revisions: list[TaskRevision],
    auto_commit: bool = True,
) -> TaskRevisionResult:
    """
    Apply in-place revisions to pending atomic tasks.

    Only tasks in PENDING status are modified. Tasks in any other status are
    skipped (returned in skipped_ids) to preserve execution integrity.
    """
    if not revisions:
        return TaskRevisionResult(revised_count=0, skipped_ids=[])

    skipped: list[int] = []
    revised_at = datetime.now(timezone.utc)

    for revision in revisions:
        task = db.get(Task, revision.task_id)

        if task is None:
            logger.warning("task_revision_skipped_not_found task_id=%s", revision.task_id)
            skipped.append(revision.task_id)
            continue

        if task.status != TASK_STATUS_PENDING:
            logger.warning(
                "task_revision_skipped_not_pending task_id=%s status=%s",
                revision.task_id,
                task.status,
            )
            skipped.append(revision.task_id)
            continue

        _apply_fields(task, revision)
        task.revised_at = revised_at

        logger.info("task_revised task_id=%s", revision.task_id)

    if auto_commit:
        db.commit()
    else:
        db.flush()

    revised_count = len(revisions) - len(skipped)
    return TaskRevisionResult(revised_count=revised_count, skipped_ids=skipped)


def _apply_fields(task: Task, revision: TaskRevision) -> None:
    """Overwrite only the fields explicitly provided in the revision."""
    if revision.description is not None:
        task.description = revision.description
    if revision.objective is not None:
        task.objective = revision.objective
    if revision.proposed_solution is not None:
        task.proposed_solution = revision.proposed_solution
    if revision.implementation_steps is not None:
        task.implementation_steps = revision.implementation_steps
    if revision.acceptance_criteria is not None:
        task.acceptance_criteria = revision.acceptance_criteria
    if revision.technical_constraints is not None:
        task.technical_constraints = revision.technical_constraints
    if revision.out_of_scope is not None:
        task.out_of_scope = revision.out_of_scope
    if revision.depends_on_task_titles is not None:
        task.depends_on_task_titles = revision.depends_on_task_titles
