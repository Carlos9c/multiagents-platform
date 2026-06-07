"""Tests for resumption_service."""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PENDING,
    TASK_STATUS_SUPERSEDED,
    Task,
)
from app.schemas.execution_plan import (
    CheckpointDefinition,
    ExecutionBatch,
    ExecutionPlan,
)
from app.services.conversation.impact_assessment_agent import (
    ImpactAssessmentResult,
    NewWorkBlock,
    TaskRevisionSpec,
)
from app.services.conversation.resumption_service import (
    ResumptionError,
    resume_after_review,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_fake_plan(task_ids: list[int], version: int = 1) -> ExecutionPlan:
    """Build a minimal valid ExecutionPlan for testing (all tasks in one batch)."""
    checkpoint_id = f"cp_{version}_final"
    batch_id = f"plan_{version}_batch_1"
    return ExecutionPlan(
        plan_version=version,
        supersedes_plan_version=None if version == 1 else version - 1,
        planning_scope="project_atomic_tasks",
        global_goal="Test resequencing after scope change",
        execution_batches=[
            ExecutionBatch(
                batch_internal_id=f"{version}_1_0",
                batch_id=batch_id,
                batch_index=1,
                plan_version=version,
                name="Batch 1",
                goal="Execute all pending tasks",
                task_ids=task_ids,
                risk_level="low",
                checkpoint_after=True,
                checkpoint_id=checkpoint_id,
                checkpoint_reason="Validate all tasks completed",
            )
        ],
        checkpoints=[
            CheckpointDefinition(
                checkpoint_id=checkpoint_id,
                name="Final checkpoint",
                reason="Stage closure",
                after_batch_id=batch_id,
                evaluation_goal="Confirm all tasks done",
                evaluation_focus=["stage_closure"],
            )
        ],
        ready_task_ids=task_ids,
        blocked_task_ids=[],
        inferred_dependencies=[],
        sequencing_rationale="Single batch resequence",
    )


# ── Fixtures ───────────────────────────────────────────────────────────────────


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
        tasks_to_eliminate=[],
        tasks_to_modify=[],
        new_work_blocks=[],
        environment_changes=[],
    )


def _moderate_assessment(
    modify_task_id: int,
    new_work_block_parent_id: int | None = None,
) -> ImpactAssessmentResult:
    blocks = []
    if new_work_block_parent_id is not None:
        blocks = [
            NewWorkBlock(
                title="New injected task",
                description="Desc",
                objective="Obj",
                proposed_solution="Sol",
                acceptance_criteria="Criteria",
                technical_constraints="None",
                out_of_scope="",
                task_type="implementation",
                depends_on_task_titles=[],
                planning_level="atomic",
                parent_task_id=new_work_block_parent_id,
                reason="Gap identified",
            )
        ]
    return ImpactAssessmentResult(
        change_scope="moderate",
        reasoning="Some pending tasks need updating.",
        tasks_to_eliminate=[],
        tasks_to_modify=[
            TaskRevisionSpec(
                task_id=modify_task_id,
                new_description="Updated description",
                new_objective=None,
                new_implementation_steps="New steps",
                new_acceptance_criteria=None,
                new_technical_constraints=None,
                new_depends_on_task_titles=None,
                reason="Affected by clarification",
            )
        ],
        new_work_blocks=blocks,
        environment_changes=[],
    )


def _moderate_with_elimination(
    eliminate_ids: list[int],
    modify_task_id: int | None = None,
) -> ImpactAssessmentResult:
    revisions = []
    if modify_task_id is not None:
        revisions = [
            TaskRevisionSpec(
                task_id=modify_task_id,
                new_description="Updated after elimination",
                new_objective=None,
                new_implementation_steps=None,
                new_acceptance_criteria=None,
                new_technical_constraints=None,
                new_depends_on_task_titles=None,
                reason="Sibling eliminated",
            )
        ]
    return ImpactAssessmentResult(
        change_scope="moderate",
        reasoning="User wants some tasks eliminated.",
        tasks_to_eliminate=eliminate_ids,
        tasks_to_modify=revisions,
        new_work_blocks=[],
        environment_changes=[],
    )


def _disruptive_assessment() -> ImpactAssessmentResult:
    return ImpactAssessmentResult(
        change_scope="disruptive",
        reasoning="High-level goals invalidated.",
        tasks_to_eliminate=[],
        tasks_to_modify=[],
        new_work_blocks=[],
        environment_changes=[],
    )


# ── Narrow tests ──────────────────────────────────────────────────────────────


def test_narrow_resets_blocked_task_to_pending(db_session, make_project):
    project = make_project()
    blocked = _make_task(
        db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=1
    )
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

    with pytest.raises(ResumptionError, match="cannot be resumed"):
        resume_after_review(
            db_session,
            project_id=project.id,
            blocked_task_id=task.id,
            user_clarification="irrelevant",
        )


# ── Moderate tests ────────────────────────────────────────────────────────────


def test_moderate_revises_pending_task_and_resets_blocked(db_session, make_project):
    project = make_project()
    blocked = _make_task(
        db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=2
    )
    pending = _make_task(db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=3)
    db_session.commit()

    with patch(
        "app.services.conversation.resumption_service.assess_impact",
        return_value=_moderate_assessment(modify_task_id=pending.id),
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
    # new_work_blocks processing is deferred to Phase 5 — tasks_added is 0 for now
    assert result.tasks_added == 0


def test_moderate_new_work_blocks_creates_atomic_tasks(db_session, make_project):
    """new_work_blocks are processed via the atomizer (Phase 5).

    The atomizer is mocked; verifies that the service calls it and the
    returned IDs are counted as tasks_added.
    """
    from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput, AtomicTaskOutput

    project = make_project(name="P", description="D")
    blocked = _make_task(
        db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=1
    )
    parent = _make_task(
        db_session,
        project.id,
        status=TASK_STATUS_PENDING,
        sequence_order=None,
        planning_level="high_level",
        title="Auth module",
    )
    db_session.commit()

    mock_atomic = AtomicTaskOutput(
        title="Implement session invalidation endpoint for auth",
        description="POST /sessions/invalidate that removes the session record from the DB",
        summary="Session invalidation",
        objective="Allow explicit logout via API",
        proposed_solution="FastAPI route + SQLAlchemy delete",
        implementation_steps=["- Add route", "- Delete session"],
        tests_required=["- Test 204 response"],
        acceptance_criteria=["Endpoint returns 204 and session is removed"],
        technical_constraints="PostgreSQL 14+",
        out_of_scope="Token blacklisting",
        priority="medium",
        task_type="implementation",
        verification_level="runtime",
        estimated_complexity="S",
        depends_on_task_titles=[],
    )
    fake_output = AtomicTaskGenerationOutput(
        generation_summary="Generated 1 atomic task for the session invalidation feature",
        atomic_tasks=[mock_atomic],
    )

    with (
        patch(
            "app.services.conversation.resumption_service.assess_impact",
            return_value=_moderate_assessment(
                modify_task_id=blocked.id, new_work_block_parent_id=parent.id
            ),
        ),
        patch(
            "app.services.conversation.resumption_service.call_atomic_task_generator_model",
            return_value=fake_output,
        ),
        patch(
            "app.services.conversation.resumption_service.call_execution_sequencer_model",
            side_effect=lambda inp, **kw: _make_fake_plan(
                [t.task_id for t in inp.candidate_atomic_tasks]
            ),
        ),
    ):
        result = resume_after_review(
            db_session,
            project_id=project.id,
            blocked_task_id=blocked.id,
            user_clarification="Add new task",
        )

    assert result.scope == "moderate"
    # Phase 5 is now active — tasks_added reflects actual atomics created
    assert result.tasks_added == 1


def test_moderate_with_tasks_to_eliminate_cancels_task(db_session, make_project):
    """tasks_to_eliminate are cancelled immediately (Phase 4 implemented)."""
    project = make_project()
    blocked = _make_task(
        db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=1
    )
    candidate = _make_task(
        db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=2, title="To remove"
    )
    db_session.commit()

    with patch(
        "app.services.conversation.resumption_service.assess_impact",
        return_value=_moderate_with_elimination(eliminate_ids=[candidate.id]),
    ):
        result = resume_after_review(
            db_session,
            project_id=project.id,
            blocked_task_id=blocked.id,
            user_clarification="Remove the second task",
        )

    db_session.refresh(candidate)
    assert result.scope == "moderate"
    assert candidate.status == TASK_STATUS_CANCELLED
    assert result.tasks_eliminated == 1


# ── Disruptive tests ──────────────────────────────────────────────────────────


def test_disruptive_supersedes_non_terminal_tasks(db_session, make_project):
    project = make_project()
    blocked = _make_task(
        db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=3
    )
    pending1 = _make_task(db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=4)
    pending2 = _make_task(db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=5)
    done = _make_task(db_session, project.id, status=TASK_STATUS_COMPLETED)
    db_session.commit()

    with (
        patch(
            "app.services.conversation.resumption_service.assess_impact",
            return_value=_disruptive_assessment(),
        ),
        patch("app.services.conversation.resumption_service.ProjectStartService") as mock_svc_class,
    ):
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
    blocked = _make_task(
        db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=1
    )
    db_session.commit()

    new_goal = "Completely revised project goal"

    with (
        patch(
            "app.services.conversation.resumption_service.assess_impact",
            return_value=_disruptive_assessment(),
        ),
        patch("app.services.conversation.resumption_service.ProjectStartService") as mock_svc_class,
    ):
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


# ── Clarifications JSON column tests ─────────────────────────────────────────


def test_embed_clarification_populates_json_list(db_session, make_project):
    from app.services.conversation.resumption_service import _embed_clarification

    project = make_project()
    task = _make_task(db_session, project.id)
    db_session.commit()

    _embed_clarification(task, "use Redis for caching", episode_index=0)

    assert task.clarifications is not None
    assert len(task.clarifications) == 1
    entry = task.clarifications[0]
    assert entry["text"] == "use Redis for caching"
    assert entry["episode_index"] == 0
    assert "created_at" in entry


def test_embed_clarification_accumulates_across_episodes(db_session, make_project):
    from app.services.conversation.resumption_service import _embed_clarification

    project = make_project()
    task = _make_task(db_session, project.id)
    db_session.commit()

    _embed_clarification(task, "use Redis", episode_index=0)
    _embed_clarification(task, "use port 6379", episode_index=1)

    assert len(task.clarifications) == 2
    assert task.clarifications[0]["episode_index"] == 0
    assert task.clarifications[1]["episode_index"] == 1


def test_embed_clarification_also_appends_to_description(db_session, make_project):
    from app.services.conversation.resumption_service import (
        _CLARIFICATION_HEADER,
        _embed_clarification,
    )

    project = make_project()
    task = _make_task(db_session, project.id, description="Original description")
    db_session.commit()

    _embed_clarification(task, "switch to Postgres", episode_index=0)

    assert _CLARIFICATION_HEADER in task.description
    assert "switch to Postgres" in task.description
    assert task.description.startswith("Original description")


def test_embed_clarification_null_clarifications_treated_as_empty(db_session, make_project):
    from app.services.conversation.resumption_service import _embed_clarification

    project = make_project()
    task = _make_task(db_session, project.id)
    assert task.clarifications is None
    db_session.commit()

    _embed_clarification(task, "add logging", episode_index=0)

    assert task.clarifications is not None
    assert len(task.clarifications) == 1


def test_narrow_resumption_writes_clarifications_json(db_session, make_project):
    project = make_project()
    blocked = _make_task(db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW)
    db_session.commit()

    with patch(
        "app.services.conversation.resumption_service.assess_impact",
        return_value=_narrow_assessment(),
    ):
        resume_after_review(
            db_session,
            project_id=project.id,
            blocked_task_id=blocked.id,
            user_clarification="The DB password is postgres123",
        )

    db_session.refresh(blocked)
    assert blocked.clarifications is not None
    assert len(blocked.clarifications) == 1
    assert blocked.clarifications[0]["text"] == "The DB password is postgres123"


# ── Phase 4: cascade cancellation tests ──────────────────────────────────────


class TestCancelTasksAndCascade:
    """Unit tests for _cancel_tasks_and_cascade() Phase 4 helper."""

    def test_cancels_single_task(self, db_session, make_project):
        from app.services.conversation.resumption_service import _cancel_tasks_and_cascade

        project = make_project()
        task = _make_task(db_session, project.id, status=TASK_STATUS_PENDING)
        db_session.commit()

        cancelled = _cancel_tasks_and_cascade(db_session, project.id, [task.id])

        db_session.refresh(task)
        assert task.id in cancelled
        assert task.status == TASK_STATUS_CANCELLED

    def test_skips_already_terminal_task(self, db_session, make_project):
        from app.models.task import TASK_STATUS_COMPLETED
        from app.services.conversation.resumption_service import _cancel_tasks_and_cascade

        project = make_project()
        task = _make_task(db_session, project.id, status=TASK_STATUS_COMPLETED)
        db_session.commit()

        cancelled = _cancel_tasks_and_cascade(db_session, project.id, [task.id])

        db_session.refresh(task)
        assert task.id not in cancelled
        assert task.status == TASK_STATUS_COMPLETED  # unchanged

    def test_skips_task_from_different_project(self, db_session, make_project):
        from app.services.conversation.resumption_service import _cancel_tasks_and_cascade

        project_a = make_project()
        project_b = make_project()
        task_b = _make_task(db_session, project_b.id, status=TASK_STATUS_PENDING)
        db_session.commit()

        # Try to cancel task_b via project_a — must be silently skipped
        cancelled = _cancel_tasks_and_cascade(db_session, project_a.id, [task_b.id])

        db_session.refresh(task_b)
        assert task_b.id not in cancelled
        assert task_b.status == TASK_STATUS_PENDING  # unchanged

    def test_cascade_cancels_parent_when_all_children_cancelled(self, db_session, make_project):
        from app.services.conversation.resumption_service import _cancel_tasks_and_cascade

        project = make_project()
        parent = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            planning_level=PLANNING_LEVEL_ATOMIC,
            title="Parent",
        )
        child1 = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            parent_task_id=parent.id,
            title="Child 1",
        )
        child2 = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            parent_task_id=parent.id,
            title="Child 2",
        )
        db_session.commit()

        cancelled = _cancel_tasks_and_cascade(db_session, project.id, [child1.id, child2.id])

        db_session.refresh(parent)
        assert parent.id in cancelled
        assert parent.status == TASK_STATUS_CANCELLED

    def test_no_cascade_when_sibling_is_completed(self, db_session, make_project):
        from app.models.task import TASK_STATUS_COMPLETED
        from app.services.conversation.resumption_service import _cancel_tasks_and_cascade

        project = make_project()
        parent = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            planning_level=PLANNING_LEVEL_ATOMIC,
            title="Parent",
        )
        _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_COMPLETED,
            parent_task_id=parent.id,
            title="Done child",
        )
        pending_child = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            parent_task_id=parent.id,
            title="Pending child",
        )
        db_session.commit()

        # Cancel only the pending child — completed sibling means parent survives
        cancelled = _cancel_tasks_and_cascade(db_session, project.id, [pending_child.id])

        db_session.refresh(parent)
        assert parent.id not in cancelled
        assert parent.status == TASK_STATUS_PENDING

    def test_no_cascade_when_sibling_still_pending(self, db_session, make_project):
        from app.services.conversation.resumption_service import _cancel_tasks_and_cascade

        project = make_project()
        parent = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            planning_level=PLANNING_LEVEL_ATOMIC,
            title="Parent",
        )
        child1 = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            parent_task_id=parent.id,
            title="Child 1",
        )
        _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            parent_task_id=parent.id,
            title="Child 2 (stays pending)",
        )
        db_session.commit()

        # Cancel only child1 — child2 still pending, so parent must NOT cascade
        cancelled = _cancel_tasks_and_cascade(db_session, project.id, [child1.id])

        db_session.refresh(parent)
        assert parent.id not in cancelled
        assert parent.status == TASK_STATUS_PENDING

    def test_cascade_chain_two_levels(self, db_session, make_project):
        """Grandparent is cancelled when parent and all children cascade."""
        from app.services.conversation.resumption_service import _cancel_tasks_and_cascade

        project = make_project()
        grandparent = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            planning_level=PLANNING_LEVEL_ATOMIC,
            title="Grandparent",
        )
        parent = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            planning_level=PLANNING_LEVEL_ATOMIC,
            title="Parent",
            parent_task_id=grandparent.id,
        )
        child = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            parent_task_id=parent.id,
            title="Child",
        )
        db_session.commit()

        cancelled = _cancel_tasks_and_cascade(db_session, project.id, [child.id])

        db_session.refresh(parent)
        db_session.refresh(grandparent)
        assert parent.id in cancelled
        assert parent.status == TASK_STATUS_CANCELLED
        assert grandparent.id in cancelled
        assert grandparent.status == TASK_STATUS_CANCELLED

    def test_empty_list_returns_empty_set(self, db_session, make_project):
        from app.services.conversation.resumption_service import _cancel_tasks_and_cascade

        project = make_project()
        db_session.commit()

        result = _cancel_tasks_and_cascade(db_session, project.id, [])
        assert result == set()


# ── Phase 4: integration tests (moderate scope with cancellation) ─────────────


class TestModerateScopeCancellation:
    """Integration tests for the full moderate path with tasks_to_eliminate."""

    def test_moderate_cancels_specified_tasks(self, db_session, make_project):
        project = make_project()
        blocked = _make_task(
            db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=1
        )
        to_cancel = _make_task(db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=2)
        other = _make_task(db_session, project.id, status=TASK_STATUS_PENDING, sequence_order=3)
        db_session.commit()

        with patch(
            "app.services.conversation.resumption_service.assess_impact",
            return_value=_moderate_with_elimination(eliminate_ids=[to_cancel.id]),
        ):
            result = resume_after_review(
                db_session,
                project_id=project.id,
                blocked_task_id=blocked.id,
                user_clarification="Remove that task",
            )

        db_session.refresh(to_cancel)
        db_session.refresh(other)

        assert result.scope == "moderate"
        assert to_cancel.status == TASK_STATUS_CANCELLED
        assert other.status == TASK_STATUS_PENDING  # untouched
        assert result.tasks_eliminated == 1

    def test_moderate_cascade_cancels_parent(self, db_session, make_project):
        project = make_project()
        blocked = _make_task(
            db_session, project.id, status=TASK_STATUS_AWAITING_REVIEW, sequence_order=1
        )
        parent = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            planning_level=PLANNING_LEVEL_ATOMIC,
            title="Feature parent",
            sequence_order=None,
        )
        child1 = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            parent_task_id=parent.id,
            sequence_order=2,
        )
        child2 = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            parent_task_id=parent.id,
            sequence_order=3,
        )
        db_session.commit()

        with patch(
            "app.services.conversation.resumption_service.assess_impact",
            return_value=_moderate_with_elimination(eliminate_ids=[child1.id, child2.id]),
        ):
            result = resume_after_review(
                db_session,
                project_id=project.id,
                blocked_task_id=blocked.id,
                user_clarification="Remove the whole feature",
            )

        db_session.refresh(parent)
        db_session.refresh(child1)
        db_session.refresh(child2)

        assert child1.status == TASK_STATUS_CANCELLED
        assert child2.status == TASK_STATUS_CANCELLED
        assert parent.status == TASK_STATUS_CANCELLED
        # Parent also counted as eliminated (cascade)
        assert result.tasks_eliminated == 3


# ── Phase 5: new work creation tests ─────────────────────────────────────────


class TestCreateWorkFromBlocks:
    """Unit tests for _create_work_from_blocks() Phase 5 helper."""

    def test_high_level_path_creates_parent_and_calls_atomizer(self, db_session, make_project):
        from unittest.mock import patch

        from app.services.conversation.resumption_service import _create_work_from_blocks

        project = make_project(name="Test project", description="A test project")
        db_session.commit()

        block = NewWorkBlock(
            title="Implement caching layer",
            description="Add Memcached caching to reduce DB load",
            objective="Reduce average query time by 50%",
            proposed_solution="Use pymemcache with TTL-based invalidation",
            acceptance_criteria="Cache hit rate exceeds 70% in load test",
            technical_constraints="Memcached 1.6+, pymemcache 4+",
            out_of_scope="Persistent storage, Redis",
            task_type="implementation",
            depends_on_task_titles=[],
            planning_level="high_level",
            parent_task_id=None,
            reason="User replaced Redis requirement with Memcached",
        )

        fake_atomizer_result = {"atomic_task_ids": [101, 102], "tasks_created": 2}

        with patch(
            "app.services.conversation.resumption_service.generate_atomic_tasks",
            return_value=fake_atomizer_result,
        ) as mock_gen:
            ids = _create_work_from_blocks(db_session, project, [block])

        assert ids == [101, 102]
        mock_gen.assert_called_once()
        call_kwargs = mock_gen.call_args
        # The new high_level parent must have been created and passed as task_id
        assert call_kwargs.kwargs["project_id"] == project.id

        # Verify the parent Task was created in DB
        from app.models.task import Task

        parent = (
            db_session.query(Task)
            .filter(Task.project_id == project.id, Task.planning_level == PLANNING_LEVEL_HIGH_LEVEL)
            .first()
        )
        assert parent is not None
        assert parent.title == "Implement caching layer"
        assert parent.depends_on_task_titles == []

    def test_atomic_path_calls_inline_atomizer_and_creates_tasks(self, db_session, make_project):
        from unittest.mock import patch

        from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput, AtomicTaskOutput
        from app.services.conversation.resumption_service import _create_work_from_blocks

        project = make_project(name="API project", description="REST API")
        existing_parent = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            planning_level=PLANNING_LEVEL_ATOMIC,
            title="Auth module",
        )
        db_session.commit()

        block = NewWorkBlock(
            title="Add session invalidation endpoint",
            description="POST /sessions/invalidate that removes a session from DB",
            objective="Allow users to log out explicitly",
            proposed_solution="Delete session record, return 204",
            acceptance_criteria="Endpoint returns 204 and session is gone from DB",
            technical_constraints="PostgreSQL 14+",
            out_of_scope="Token blacklisting",
            task_type="implementation",
            depends_on_task_titles=["Setup sessions table"],
            planning_level="atomic",
            parent_task_id=existing_parent.id,
            reason="Needed for explicit logout flow",
        )

        mock_atomic = AtomicTaskOutput(
            title="Implement POST /sessions/invalidate handler",
            description="Create the FastAPI route and DB delete logic for session invalidation",
            summary="Session invalidation endpoint",
            objective="Allow explicit logout via API",
            proposed_solution="FastAPI route + SQLAlchemy delete",
            implementation_steps=["- Add route", "- Add DB delete"],
            tests_required=["- Test 204 response", "- Test session removed from DB"],
            acceptance_criteria=["Endpoint returns 204", "Session record deleted from DB"],
            technical_constraints="PostgreSQL 14+, SQLAlchemy 2.0",
            out_of_scope="Token blacklisting, Redis",
            priority="medium",
            task_type="implementation",
            verification_level="runtime",
            estimated_complexity="S",
            depends_on_task_titles=["Setup sessions table"],
        )
        fake_output = AtomicTaskGenerationOutput(
            generation_summary="Generated 1 atomic task for session invalidation",
            atomic_tasks=[mock_atomic],
        )

        with patch(
            "app.services.conversation.resumption_service.call_atomic_task_generator_model",
            return_value=fake_output,
        ) as mock_call:
            ids = _create_work_from_blocks(db_session, project, [block])

        assert len(ids) == 1
        mock_call.assert_called_once()
        call_kwargs = mock_call.call_args.kwargs
        assert call_kwargs["call_type"] == "scope_change"
        assert call_kwargs["parent_task_id"] == existing_parent.id

        # Verify the created atomic task is anchored to the existing parent
        from app.models.task import Task

        created = db_session.get(Task, ids[0])
        assert created is not None
        assert created.parent_task_id == existing_parent.id
        assert created.planning_level == PLANNING_LEVEL_ATOMIC
        assert created.status == TASK_STATUS_PENDING

    def test_empty_blocks_returns_empty_list(self, db_session, make_project):
        from app.services.conversation.resumption_service import _create_work_from_blocks

        project = make_project()
        db_session.commit()

        result = _create_work_from_blocks(db_session, project, [])
        assert result == []

    def test_sibling_summary_updated_across_blocks(self, db_session, make_project):
        """Each block gets a sibling summary that includes the previous block's atomics."""
        from unittest.mock import patch

        from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput, AtomicTaskOutput
        from app.services.conversation.resumption_service import _create_work_from_blocks

        project = make_project(name="P", description="D")
        parent = _make_task(
            db_session,
            project.id,
            status=TASK_STATUS_PENDING,
            planning_level=PLANNING_LEVEL_ATOMIC,
            title="Feature",
        )
        db_session.commit()

        def _make_atomic_output(title: str) -> AtomicTaskGenerationOutput:
            return AtomicTaskGenerationOutput(
                generation_summary=f"Generated task: {title}",
                atomic_tasks=[
                    AtomicTaskOutput(
                        title=title,
                        description=f"Detailed description of {title} implementation",
                        summary=f"Summary of {title}",
                        objective=f"Objective of {title}",
                        proposed_solution=f"Proposed solution for {title} using standard patterns",
                        implementation_steps=["- Step 1", "- Step 2"],
                        tests_required=["- Test 1"],
                        acceptance_criteria=[f"{title} works correctly in production"],
                        technical_constraints="Python 3.12+",
                        out_of_scope="Out of scope items",
                        priority="medium",
                        task_type="implementation",
                        verification_level="runtime",
                        estimated_complexity="S",
                        depends_on_task_titles=[],
                    )
                ],
            )

        captured_siblings: list[list] = []

        def _capture_call(**kwargs):
            captured_siblings.append(kwargs.get("sibling_atomic_summary") or [])
            title = kwargs["parent_task_title"]
            return _make_atomic_output(f"Atomic for {title}")

        blocks = [
            NewWorkBlock(
                title="Block A",
                description="First block description",
                objective="First block objective",
                proposed_solution="Solution A",
                acceptance_criteria="Block A works",
                technical_constraints="Python 3.12",
                out_of_scope="Nothing",
                task_type="implementation",
                depends_on_task_titles=[],
                planning_level="atomic",
                parent_task_id=parent.id,
                reason="Reason A",
            ),
            NewWorkBlock(
                title="Block B",
                description="Second block description",
                objective="Second block objective",
                proposed_solution="Solution B",
                acceptance_criteria="Block B works",
                technical_constraints="Python 3.12",
                out_of_scope="Nothing",
                task_type="implementation",
                depends_on_task_titles=[],
                planning_level="atomic",
                parent_task_id=parent.id,
                reason="Reason B",
            ),
        ]

        with patch(
            "app.services.conversation.resumption_service.call_atomic_task_generator_model",
            side_effect=_capture_call,
        ):
            _create_work_from_blocks(db_session, project, blocks)

        # Second call should have at least the task from Block A in sibling summary
        assert len(captured_siblings) == 2
        second_call_titles = [s["title"] for s in captured_siblings[1]]
        assert any("Block A" in t for t in second_call_titles)
