"""Phase 6 + Phase 7 integration tests for the full moderate scope pipeline.

These tests cover the complete flow from resume_after_review() through to DB state,
verifying that all phases (cancel → modify → create → resequence) work together.

All LLM calls are mocked (no real API keys required).
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PENDING,
    Task,
)
from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput, AtomicTaskOutput
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
    _build_pending_candidates,
    _reintegrate_plan,
    _resequence_with_sequencer,
    resume_after_review,
)

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _make_task(
    db: Session,
    project_id: int,
    *,
    title: str = "A task",
    status: str = TASK_STATUS_PENDING,
    planning_level: str = PLANNING_LEVEL_ATOMIC,
    parent_task_id: int | None = None,
    sequence_order: int | None = None,
    depends_on_task_titles: list | None = None,
    description: str = "Task description",
) -> Task:
    task = Task(
        project_id=project_id,
        title=title,
        description=description,
        planning_level=planning_level,
        status=status,
        parent_task_id=parent_task_id,
        sequence_order=sequence_order,
        depends_on_task_titles=depends_on_task_titles or [],
        task_type="implementation",
        priority="medium",
    )
    db.add(task)
    db.flush()
    return task


def _make_atomic_output(title: str) -> AtomicTaskOutput:
    return AtomicTaskOutput(
        title=title,
        description=f"Detailed description for {title} implementation task",
        summary=f"Summary: {title}",
        objective=f"Objective: implement {title} correctly",
        proposed_solution=f"Standard implementation approach for {title}",
        implementation_steps=["- Step 1: analysis", "- Step 2: implementation"],
        tests_required=["- Test 1: happy path", "- Test 2: error case"],
        acceptance_criteria=[f"{title} works as expected in all test cases"],
        technical_constraints="Python 3.12+, follows project conventions",
        out_of_scope="External dependencies and third-party integrations",
        priority="medium",
        task_type="implementation",
        verification_level="runtime",
        estimated_complexity="S",
        depends_on_task_titles=[],
    )


def _fake_atomizer_output(titles: list[str]) -> AtomicTaskGenerationOutput:
    return AtomicTaskGenerationOutput(
        generation_summary=f"Generated {len(titles)} atomic task(s) for the scope change",
        atomic_tasks=[_make_atomic_output(t) for t in titles],
    )


def _fake_plan(task_ids: list[int], *, batches: int = 1) -> ExecutionPlan:
    """Build a valid multi-batch plan for testing dependency-aware resequencing.

    Distributes task_ids across up to `batches` batches. If len(task_ids) < batches,
    fewer batches are created. The final batch always has stage_closure in its checkpoint.
    """
    if not task_ids:
        task_ids = [-1]  # avoid empty task_ids (edge case in tests)

    # Distribute task_ids into chunks
    actual_batches = min(batches, len(task_ids))
    batch_size = len(task_ids) // actual_batches
    chunks: list[list[int]] = []
    for i in range(actual_batches):
        start = i * batch_size
        end = start + batch_size if i < actual_batches - 1 else len(task_ids)
        chunks.append(task_ids[start:end])
    # Filter out empty chunks (safety net)
    chunks = [c for c in chunks if c]

    execution_batches = []
    checkpoints = []

    for idx, chunk in enumerate(chunks, start=1):
        batch_id = f"plan_1_batch_{idx}"
        cp_id = f"cp_{idx}"
        is_final = idx == len(chunks)
        execution_batches.append(
            ExecutionBatch(
                batch_internal_id=f"1_{idx}_0",
                batch_id=batch_id,
                batch_index=idx,
                plan_version=1,
                name=f"Batch {idx}",
                goal=f"Execute batch {idx}",
                task_ids=chunk,
                risk_level="low",
                checkpoint_after=True,
                checkpoint_id=cp_id,
                checkpoint_reason=f"Validate batch {idx}",
            )
        )
        checkpoints.append(
            CheckpointDefinition(
                checkpoint_id=cp_id,
                name=f"Checkpoint {idx}",
                reason="Batch validation",
                after_batch_id=batch_id,
                evaluation_goal="Confirm tasks done",
                evaluation_focus=(
                    ["task_completion_quality", "stage_closure"]
                    if is_final
                    else ["task_completion_quality"]
                ),
            )
        )

    return ExecutionPlan(
        plan_version=1,
        supersedes_plan_version=None,
        planning_scope="project_atomic_tasks",
        global_goal="Test resequencing",
        execution_batches=execution_batches,
        checkpoints=checkpoints,
        ready_task_ids=task_ids,
        blocked_task_ids=[],
        inferred_dependencies=[],
        sequencing_rationale="Test plan for resequencing validation",
    )


# ── Phase 6: _reintegrate_plan unit tests ─────────────────────────────────────


class TestReintegratePlan:
    """Unit tests for the _reintegrate_plan routing function."""

    def test_calls_sequencer_when_new_atomics_added(self, db_session, make_project):
        project = make_project(name="P", description="D")
        _make_task(db_session, project.id, title="Task 1", sequence_order=1)
        t2 = _make_task(db_session, project.id, title="Task 2", sequence_order=2)
        db_session.commit()

        sequencer_called = []

        def _mock_sequencer(inp, **kw):
            sequencer_called.append(True)
            return _fake_plan([t.task_id for t in inp.candidate_atomic_tasks])

        with patch(
            "app.services.conversation.resumption_service.call_execution_sequencer_model",
            side_effect=_mock_sequencer,
        ):
            _reintegrate_plan(
                db_session,
                project,
                new_atomic_ids=[t2.id],
                cancelled_ids=set(),
                depends_changed=False,
            )

        assert len(sequencer_called) == 1, "Sequencer must be called when new atomics exist"

    def test_mechanical_resequence_when_only_cancellations(self, db_session, make_project):
        project = make_project(name="P", description="D")
        t1 = _make_task(db_session, project.id, title="Task 1", sequence_order=1)
        t2 = _make_task(db_session, project.id, title="Task 3", sequence_order=3)
        db_session.commit()

        sequencer_called = []

        with patch(
            "app.services.conversation.resumption_service.call_execution_sequencer_model",
            side_effect=lambda *a, **kw: sequencer_called.append(True),
        ):
            _reintegrate_plan(
                db_session,
                project,
                new_atomic_ids=[],
                cancelled_ids={99},  # some task was cancelled
                depends_changed=False,
            )

        assert len(sequencer_called) == 0, "Sequencer must NOT be called for cancellation-only"

        # Mechanical renumber should have closed the gap (3 → 2)
        db_session.refresh(t1)
        db_session.refresh(t2)
        assert t1.sequence_order == 1
        assert t2.sequence_order == 2

    def test_no_resequence_when_nothing_changed(self, db_session, make_project):
        project = make_project(name="P", description="D")
        t1 = _make_task(db_session, project.id, title="Task 1", sequence_order=5)
        db_session.commit()

        sequencer_called = []

        with patch(
            "app.services.conversation.resumption_service.call_execution_sequencer_model",
            side_effect=lambda *a, **kw: sequencer_called.append(True),
        ):
            _reintegrate_plan(
                db_session,
                project,
                new_atomic_ids=[],
                cancelled_ids=set(),
                depends_changed=False,
            )

        assert len(sequencer_called) == 0
        db_session.refresh(t1)
        assert t1.sequence_order == 5  # unchanged

    def test_sequencer_updates_sequence_order_in_db(self, db_session, make_project):
        """sequence_order is updated from the plan's batch positions."""
        project = make_project(name="P", description="D")
        t1 = _make_task(db_session, project.id, title="Task A", sequence_order=10)
        t2 = _make_task(db_session, project.id, title="Task B", sequence_order=20)
        new_id = 999  # fake new atomic id
        db_session.commit()

        # Plan places t1 in batch 1, t2 in batch 2
        two_batch_plan = _fake_plan([t1.id, t2.id], batches=2)

        with patch(
            "app.services.conversation.resumption_service.call_execution_sequencer_model",
            return_value=two_batch_plan,
        ):
            _reintegrate_plan(
                db_session,
                project,
                new_atomic_ids=[new_id],
                cancelled_ids=set(),
                depends_changed=False,
            )

        db_session.refresh(t1)
        db_session.refresh(t2)
        assert t1.sequence_order == 1  # batch 1
        assert t2.sequence_order == 2  # batch 2


# ── Phase 6: _resequence_with_sequencer unit tests ────────────────────────────


class TestResequenceWithSequencer:
    def test_skips_when_no_pending_atomics(self, db_session, make_project):
        project = make_project(name="P", description="D")
        db_session.commit()

        sequencer_called = []

        with patch(
            "app.services.conversation.resumption_service.call_execution_sequencer_model",
            side_effect=lambda *a, **kw: sequencer_called.append(True),
        ):
            _resequence_with_sequencer(db_session, project)

        assert len(sequencer_called) == 0

    def test_builds_candidates_from_pending_atomics_only(self, db_session, make_project):
        project = make_project(name="P", description="D")
        pending = _make_task(db_session, project.id, title="Pending task", sequence_order=1)
        _make_task(
            db_session,
            project.id,
            title="Completed task",
            status=TASK_STATUS_COMPLETED,
            sequence_order=0,
        )
        _make_task(
            db_session,
            project.id,
            title="High level parent",
            planning_level=PLANNING_LEVEL_HIGH_LEVEL,
            sequence_order=None,
        )
        db_session.commit()

        captured_input = []

        def _capture(inp, **kw):
            captured_input.append(inp)
            return _fake_plan([t.task_id for t in inp.candidate_atomic_tasks])

        with patch(
            "app.services.conversation.resumption_service.call_execution_sequencer_model",
            side_effect=_capture,
        ):
            _resequence_with_sequencer(db_session, project)

        assert len(captured_input) == 1
        candidate_ids = [c.task_id for c in captured_input[0].candidate_atomic_tasks]
        assert pending.id in candidate_ids
        # High-level and completed tasks must NOT appear in candidates
        assert all(
            c.planning_level == PLANNING_LEVEL_ATOMIC
            for c in captured_input[0].candidate_atomic_tasks
        )

    def test_call_type_is_resequence(self, db_session, make_project):
        project = make_project(name="P", description="D")
        _make_task(db_session, project.id, title="Task", sequence_order=1)
        db_session.commit()

        captured_kwargs = []

        def _capture(inp, **kw):
            captured_kwargs.append(kw)
            return _fake_plan([t.task_id for t in inp.candidate_atomic_tasks])

        with patch(
            "app.services.conversation.resumption_service.call_execution_sequencer_model",
            side_effect=_capture,
        ):
            _resequence_with_sequencer(db_session, project)

        assert captured_kwargs[0].get("call_type") == "resequence"


# ── Phase 6: _build_pending_candidates unit tests ────────────────────────────


class TestBuildPendingCandidates:
    def test_returns_only_pending_atomics(self, db_session, make_project):
        project = make_project()
        pending = _make_task(db_session, project.id, title="Pending", sequence_order=1)
        _make_task(db_session, project.id, title="Completed", status=TASK_STATUS_COMPLETED)
        _make_task(
            db_session, project.id, title="High-level", planning_level=PLANNING_LEVEL_HIGH_LEVEL
        )
        db_session.commit()

        candidates = _build_pending_candidates(db_session, project.id)

        assert len(candidates) == 1
        assert candidates[0].task_id == pending.id
        assert candidates[0].planning_level == PLANNING_LEVEL_ATOMIC

    def test_resolves_parent_title(self, db_session, make_project):
        project = make_project()
        parent = _make_task(
            db_session, project.id, title="Auth module", planning_level=PLANNING_LEVEL_HIGH_LEVEL
        )
        child = _make_task(
            db_session, project.id, title="Setup JWT", parent_task_id=parent.id, sequence_order=1
        )
        db_session.commit()

        candidates = _build_pending_candidates(db_session, project.id)

        assert len(candidates) == 1
        assert candidates[0].task_id == child.id
        assert candidates[0].parent_high_level_title == "Auth module"

    def test_preserves_depends_on_task_titles(self, db_session, make_project):
        project = make_project()
        _make_task(
            db_session,
            project.id,
            title="Task B",
            sequence_order=2,
            depends_on_task_titles=["Task A"],
        )
        db_session.commit()

        candidates = _build_pending_candidates(db_session, project.id)

        assert len(candidates) == 1
        assert "Task A" in candidates[0].depends_on_task_titles

    def test_empty_project_returns_empty_list(self, db_session, make_project):
        project = make_project()
        db_session.commit()

        candidates = _build_pending_candidates(db_session, project.id)
        assert candidates == []


# ── Phase 7: Full pipeline end-to-end integration tests ──────────────────────


class TestFullPipelineEndToEnd:
    """End-to-end tests for the complete moderate scope pipeline.

    Tests the full flow from resume_after_review() through cancellation,
    modification, atomizer-based creation, and sequencer-based reordering.
    All LLM calls are mocked.
    """

    def test_full_moderate_cancel_and_create(self, db_session, make_project):
        """Complete pipeline: cancel 1 task, create 1 atomic block, resequence."""
        project = make_project(name="Auth API", description="REST API with authentication")
        blocked = _make_task(
            db_session,
            project.id,
            title="Setup Redis sessions",
            status=TASK_STATUS_AWAITING_REVIEW,
            sequence_order=1,
        )
        to_cancel = _make_task(
            db_session, project.id, title="Configure Redis client", sequence_order=2
        )
        parent = _make_task(
            db_session, project.id, title="Auth module", planning_level=PLANNING_LEVEL_HIGH_LEVEL
        )
        db_session.commit()

        assessment = ImpactAssessmentResult(
            change_scope="moderate",
            reasoning="Replacing Redis with DB sessions",
            tasks_to_eliminate=[to_cancel.id],
            tasks_to_modify=[],
            new_work_blocks=[
                NewWorkBlock(
                    title="Implement DB-based session store",
                    description="Replace Redis with PostgreSQL session table",
                    objective="Store sessions in the DB without Redis dependency",
                    proposed_solution="SQLAlchemy sessions table with TTL",
                    acceptance_criteria="Sessions persist across restarts",
                    technical_constraints="PostgreSQL 14+",
                    out_of_scope="Redis, in-memory caching",
                    task_type="implementation",
                    depends_on_task_titles=[],
                    planning_level="atomic",
                    parent_task_id=parent.id,
                    reason="Redis eliminated, need DB alternative",
                )
            ],
            environment_changes=[],
        )

        atomizer_output = _fake_atomizer_output(["Create sessions table and model"])

        def _sequencer(inp, **kw):
            task_ids = [t.task_id for t in inp.candidate_atomic_tasks]
            return _fake_plan(task_ids)

        with (
            patch(
                "app.services.conversation.resumption_service.assess_impact",
                return_value=assessment,
            ),
            patch(
                "app.services.conversation.resumption_service.call_atomic_task_generator_model",
                return_value=atomizer_output,
            ),
            patch(
                "app.services.conversation.resumption_service.call_execution_sequencer_model",
                side_effect=_sequencer,
            ),
        ):
            result = resume_after_review(
                db_session,
                project_id=project.id,
                blocked_task_id=blocked.id,
                user_clarification="Use DB sessions instead of Redis",
            )

        db_session.refresh(to_cancel)
        db_session.refresh(blocked)

        assert result.scope == "moderate"
        assert result.tasks_eliminated == 1
        assert result.tasks_added == 1
        assert to_cancel.status == TASK_STATUS_CANCELLED
        assert blocked.status == TASK_STATUS_PENDING

        # New atomic was created under the correct parent
        new_atomic = (
            db_session.query(Task)
            .filter(
                Task.project_id == project.id,
                Task.planning_level == PLANNING_LEVEL_ATOMIC,
                Task.parent_task_id == parent.id,
                Task.title == "Create sessions table and model",
            )
            .first()
        )
        assert new_atomic is not None
        assert new_atomic.status == TASK_STATUS_PENDING

    def test_full_moderate_high_level_block(self, db_session, make_project):
        """Pipeline with planning_level=high_level — atomizer via generate_atomic_tasks."""
        project = make_project(name="E-commerce API", description="E-commerce backend")
        blocked = _make_task(
            db_session,
            project.id,
            title="Configure payment gateway",
            status=TASK_STATUS_AWAITING_REVIEW,
            sequence_order=1,
        )
        db_session.commit()

        assessment = ImpactAssessmentResult(
            change_scope="moderate",
            reasoning="Adding a new shipping module",
            tasks_to_eliminate=[],
            tasks_to_modify=[],
            new_work_blocks=[
                NewWorkBlock(
                    title="Shipping and delivery module",
                    description="Handle shipping rates, carriers, and delivery tracking",
                    objective="Complete end-to-end shipping flow",
                    proposed_solution="EasyPost API integration",
                    acceptance_criteria="Shipping rates fetched, labels created",
                    technical_constraints="EasyPost SDK",
                    out_of_scope="Returns and refunds",
                    task_type="implementation",
                    depends_on_task_titles=[],
                    planning_level="high_level",
                    parent_task_id=None,
                    reason="User added shipping requirement",
                )
            ],
            environment_changes=[],
        )

        # generate_atomic_tasks creates a real parent + returns IDs
        # We mock it to avoid DB complexity
        fake_atomic_ids: list[int] = []

        def _mock_generate_atomics(db, *, project_id, task_id, **kw):
            # Simulate atomizer creating 2 atomic children
            for title in ["Setup EasyPost SDK", "Implement shipping rate endpoint"]:
                t = Task(
                    project_id=project_id,
                    parent_task_id=task_id,
                    title=title,
                    description=f"Detailed description for {title} in the shipping module",
                    planning_level=PLANNING_LEVEL_ATOMIC,
                    status=TASK_STATUS_PENDING,
                    task_type="implementation",
                    priority="medium",
                )
                db.add(t)
            db.flush()
            atomics = (
                db.query(Task)
                .filter(
                    Task.parent_task_id == task_id, Task.planning_level == PLANNING_LEVEL_ATOMIC
                )
                .all()
            )
            fake_atomic_ids.extend(t.id for t in atomics)
            db.commit()
            return {"atomic_task_ids": [t.id for t in atomics], "tasks_created": len(atomics)}

        def _sequencer(inp, **kw):
            return _fake_plan([t.task_id for t in inp.candidate_atomic_tasks])

        with (
            patch(
                "app.services.conversation.resumption_service.assess_impact",
                return_value=assessment,
            ),
            patch(
                "app.services.conversation.resumption_service.generate_atomic_tasks",
                side_effect=_mock_generate_atomics,
            ),
            patch(
                "app.services.conversation.resumption_service.call_execution_sequencer_model",
                side_effect=_sequencer,
            ),
        ):
            result = resume_after_review(
                db_session,
                project_id=project.id,
                blocked_task_id=blocked.id,
                user_clarification="Add shipping module with EasyPost",
            )

        assert result.scope == "moderate"
        assert result.tasks_added == 2

        # High-level parent was created
        hl = (
            db_session.query(Task)
            .filter(
                Task.project_id == project.id,
                Task.planning_level == PLANNING_LEVEL_HIGH_LEVEL,
                Task.title == "Shipping and delivery module",
            )
            .first()
        )
        assert hl is not None

    def test_moderate_only_modifications_no_sequencer(self, db_session, make_project):
        """Only modifications: sequencer is NOT called (no new atomics)."""
        project = make_project(name="P", description="D")
        blocked = _make_task(
            db_session,
            project.id,
            title="Configure DB",
            status=TASK_STATUS_AWAITING_REVIEW,
            sequence_order=1,
        )
        pending = _make_task(db_session, project.id, title="Write migrations", sequence_order=2)
        db_session.commit()

        assessment = ImpactAssessmentResult(
            change_scope="moderate",
            reasoning="Update migration task description",
            tasks_to_eliminate=[],
            tasks_to_modify=[
                TaskRevisionSpec(
                    task_id=pending.id,
                    new_description="Use PostgreSQL instead of SQLite",
                    new_objective=None,
                    new_implementation_steps=None,
                    new_acceptance_criteria=None,
                    new_technical_constraints=None,
                    new_depends_on_task_titles=None,
                    reason="DB changed to PostgreSQL",
                )
            ],
            new_work_blocks=[],
            environment_changes=[],
        )

        sequencer_called = []

        with (
            patch(
                "app.services.conversation.resumption_service.assess_impact",
                return_value=assessment,
            ),
            patch(
                "app.services.conversation.resumption_service.call_execution_sequencer_model",
                side_effect=lambda *a, **kw: sequencer_called.append(True),
            ),
        ):
            result = resume_after_review(
                db_session,
                project_id=project.id,
                blocked_task_id=blocked.id,
                user_clarification="Use PostgreSQL",
            )

        assert len(sequencer_called) == 0, "Sequencer must NOT be called for modify-only"
        assert result.tasks_modified == 1

        db_session.refresh(pending)
        assert pending.description == "Use PostgreSQL instead of SQLite"

    def test_moderate_cascade_and_create_sequence_order(self, db_session, make_project):
        """Sequence order is correctly updated after cancel+create."""
        project = make_project(name="P", description="D")
        blocked = _make_task(
            db_session,
            project.id,
            title="Use Redis for sessions",
            status=TASK_STATUS_AWAITING_REVIEW,
            sequence_order=1,
        )
        parent = _make_task(
            db_session, project.id, title="Session module", planning_level=PLANNING_LEVEL_HIGH_LEVEL
        )
        c1 = _make_task(
            db_session,
            project.id,
            title="Install redis-py",
            parent_task_id=parent.id,
            sequence_order=2,
        )
        c2 = _make_task(
            db_session,
            project.id,
            title="Configure Redis connection",
            parent_task_id=parent.id,
            sequence_order=3,
        )
        db_session.commit()

        assessment = ImpactAssessmentResult(
            change_scope="moderate",
            reasoning="Replacing Redis with DB",
            tasks_to_eliminate=[c1.id, c2.id],  # cascade will cancel parent too
            tasks_to_modify=[],
            new_work_blocks=[
                NewWorkBlock(
                    title="DB session store",
                    description="PostgreSQL-based session management",
                    objective="Sessions without Redis",
                    proposed_solution="SQLAlchemy session table",
                    acceptance_criteria="Sessions work in all tests",
                    technical_constraints="PostgreSQL 14+",
                    out_of_scope="Redis",
                    task_type="implementation",
                    depends_on_task_titles=[],
                    planning_level="atomic",
                    parent_task_id=parent.id,
                    reason="Redis eliminated",
                )
            ],
            environment_changes=[],
        )

        atomizer_output = _fake_atomizer_output(["Create sessions table in PostgreSQL DB"])

        # Sequencer receives blocked+new, puts blocked at seq=1, new at seq=2
        def _ordered_sequencer(inp, **kw):
            ids = [t.task_id for t in inp.candidate_atomic_tasks]
            return _fake_plan(ids, batches=2)

        with (
            patch(
                "app.services.conversation.resumption_service.assess_impact",
                return_value=assessment,
            ),
            patch(
                "app.services.conversation.resumption_service.call_atomic_task_generator_model",
                return_value=atomizer_output,
            ),
            patch(
                "app.services.conversation.resumption_service.call_execution_sequencer_model",
                side_effect=_ordered_sequencer,
            ),
        ):
            result = resume_after_review(
                db_session,
                project_id=project.id,
                blocked_task_id=blocked.id,
                user_clarification="Replace Redis with DB sessions",
            )

        db_session.refresh(c1)
        db_session.refresh(c2)
        db_session.refresh(parent)

        assert result.tasks_eliminated == 3  # c1, c2, parent (cascade)
        assert c1.status == TASK_STATUS_CANCELLED
        assert c2.status == TASK_STATUS_CANCELLED
        assert parent.status == TASK_STATUS_CANCELLED

        # New atomic was created
        new_atomic = (
            db_session.query(Task)
            .filter(
                Task.project_id == project.id,
                Task.title == "Create sessions table in PostgreSQL DB",
            )
            .first()
        )
        assert new_atomic is not None
        # Sequence order was set by the sequencer (non-zero)
        assert new_atomic.sequence_order is not None

    def test_disruptive_scope_still_works_unchanged(self, db_session, make_project):
        """Disruptive scope uses ProjectStartService — unaffected by phases 4–6."""
        project = make_project()
        blocked = _make_task(
            db_session, project.id, title="Any task", status=TASK_STATUS_AWAITING_REVIEW
        )
        _make_task(db_session, project.id, title="Pending task")
        db_session.commit()

        disruptive = ImpactAssessmentResult(
            change_scope="disruptive",
            reasoning="Architecture pivot",
            tasks_to_eliminate=[],
            tasks_to_modify=[],
            new_work_blocks=[],
            environment_changes=[],
        )

        with (
            patch(
                "app.services.conversation.resumption_service.assess_impact",
                return_value=disruptive,
            ),
            patch("app.services.conversation.resumption_service.ProjectStartService") as MockSvc,
        ):
            mock_instance = MockSvc.return_value
            mock_instance.start.return_value = None

            result = resume_after_review(
                db_session,
                project_id=project.id,
                blocked_task_id=blocked.id,
                user_clarification="Complete pivot",
            )

        assert result.scope == "disruptive"
        mock_instance.start.assert_called_once()

    def test_narrow_scope_unchanged(self, db_session, make_project):
        """Narrow scope retries the blocked task — unaffected by phases 4–6."""
        project = make_project()
        blocked = _make_task(
            db_session, project.id, title="Failing task", status=TASK_STATUS_AWAITING_REVIEW
        )
        db_session.commit()

        narrow = ImpactAssessmentResult(
            change_scope="narrow",
            reasoning="Just retry with new info",
            tasks_to_eliminate=[],
            tasks_to_modify=[],
            new_work_blocks=[],
            environment_changes=[],
        )

        with patch(
            "app.services.conversation.resumption_service.assess_impact",
            return_value=narrow,
        ):
            result = resume_after_review(
                db_session,
                project_id=project.id,
                blocked_task_id=blocked.id,
                user_clarification="Use port 5433 instead",
            )

        db_session.refresh(blocked)
        assert result.scope == "narrow"
        assert blocked.status == TASK_STATUS_PENDING
        assert "Use port 5433 instead" in (blocked.description or "")
