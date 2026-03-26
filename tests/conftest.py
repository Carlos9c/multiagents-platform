# tests/conftest.py
import os
import sys
from pathlib import Path
from typing import Any, Callable
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("AGENTS_PROJECTS_ROOT", str(Path.cwd() / ".pytest_agents_projects"))

from app.db.base import Base
from app.models.artifact import Artifact
from app.models.execution_run import (
    EXECUTION_RUN_STATUS_FAILED,
    EXECUTION_RUN_STATUS_SUCCEEDED,
    ExecutionRun,
)
from app.models.project import Project
from app.models.task import (
    EXECUTION_ENGINE,
    PENDING_ENGINE_ROUTING_EXECUTOR,
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_PENDING,
    Task,
)
from app.schemas.evaluation import (
    EvaluatedBatchSummary,
    EvaluationReplanInstruction,
    StageEvaluationOutput,
)
from app.schemas.execution_plan import (
    CheckpointDefinition,
    ExecutionBatch,
    ExecutionPlan,
)
from app.schemas.recovery import RecoveryDecision, RecoveryTaskCreate


@pytest.fixture()
def db_session(tmp_path: Path) -> Iterator[Session]:
    db_file = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_file}",
        future=True,
    )
    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
    )

    Base.metadata.create_all(bind=engine)

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def make_project(db_session: Session) -> Callable[..., Project]:
    def _make_project(
        *,
        name: str = "Test Project",
        description: str = "Project used in tests.",
    ) -> Project:
        project = Project(
            name=name,
            description=description,
        )
        db_session.add(project)
        db_session.commit()
        db_session.refresh(project)
        return project

    return _make_project


@pytest.fixture()
def make_task(db_session: Session) -> Callable[..., Task]:
    def _make_task(
        *,
        project_id: int,
        title: str = "Test task",
        description: str = "Task description for tests.",
        parent_task_id: int | None = None,
        planning_level: str = PLANNING_LEVEL_ATOMIC,
        status: str = TASK_STATUS_PENDING,
        executor_type: str = EXECUTION_ENGINE,
        sequence_order: int | None = None,
        task_type: str = "implementation",
        priority: str = "medium",
        implementation_notes: str | None = None,
        objective: str | None = None,
        acceptance_criteria: str | None = "Must satisfy the intended behavior.",
        technical_constraints: str | None = None,
        out_of_scope: str | None = None,
        is_blocked: bool = False,
        blocking_reason: str | None = None,
    ) -> Task:
        task = Task(
            project_id=project_id,
            parent_task_id=parent_task_id,
            title=title,
            description=description,
            summary=description,
            objective=objective or description,
            proposed_solution=implementation_notes,
            implementation_notes=implementation_notes,
            acceptance_criteria=acceptance_criteria,
            technical_constraints=technical_constraints,
            out_of_scope=out_of_scope,
            priority=priority,
            task_type=task_type,
            planning_level=planning_level,
            executor_type=executor_type,
            sequence_order=sequence_order,
            status=status,
            is_blocked=is_blocked,
            blocking_reason=blocking_reason,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        return task

    return _make_task


@pytest.fixture()
def make_execution_run(db_session: Session) -> Callable[..., ExecutionRun]:
    def _make_execution_run(
        *,
        task_id: int,
        status: str = EXECUTION_RUN_STATUS_SUCCEEDED,
        failure_type: str | None = None,
        failure_code: str | None = None,
        recovery_action: str | None = None,
        work_summary: str | None = "Execution finished.",
        work_details: str | None = "Execution details.",
        completed_scope: str | None = None,
        remaining_scope: str | None = None,
        blockers_found: str | None = None,
        validation_notes: str | None = None,
        error_message: str | None = None,
        input_snapshot: str | None = "input",
        output_snapshot: str | None = "output",
        execution_agent_sequence: str | None = None,
        artifacts_created: str | None = None,
    ) -> ExecutionRun:
        run = ExecutionRun(
            task_id=task_id,
            agent_name="test-agent",
            status=status,
            failure_type=failure_type,
            failure_code=failure_code,
            recovery_action=recovery_action,
            work_summary=work_summary,
            work_details=work_details,
            execution_agent_sequence=execution_agent_sequence,
            artifacts_created=artifacts_created,
            completed_scope=completed_scope,
            remaining_scope=remaining_scope,
            blockers_found=blockers_found,
            validation_notes=validation_notes,
            error_message=error_message,
            input_snapshot=input_snapshot,
            output_snapshot=output_snapshot,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)
        return run

    return _make_execution_run


@pytest.fixture()
def make_artifact(db_session: Session) -> Callable[..., Artifact]:
    def _make_artifact(
        *,
        project_id: int,
        artifact_type: str,
        content: str,
        task_id: int | None = None,
        created_by: str = "pytest",
    ) -> Artifact:
        artifact = Artifact(
            project_id=project_id,
            task_id=task_id,
            artifact_type=artifact_type,
            content=content,
            created_by=created_by,
        )
        db_session.add(artifact)
        db_session.commit()
        db_session.refresh(artifact)
        return artifact

    return _make_artifact


@pytest.fixture()
def make_stage_evaluation_output() -> Callable[..., StageEvaluationOutput]:
    def _make_stage_evaluation_output(
        *,
        decision: str = "stage_incomplete",
        decision_summary: str = "The stage is not yet closed and execution should continue.",
        project_stage_closed: bool = False,
        stage_goals_satisfied: bool = False,
        recovery_strategy: str = "none",
        recovery_reason: str | None = None,
        replan_required: bool = False,
        replan_level: str | None = None,
        replan_reason: str | None = None,
        followup_atomic_tasks_required: bool = False,
        followup_atomic_tasks_reason: str | None = None,
        manual_review_required: bool = False,
        manual_review_reason: str | None = None,
        recommended_next_action: str | None = None,
        recommended_next_action_reason: str | None = None,
        evaluated_batch_id: str = "batch_1",
        evaluated_outcome: str = "successful",
        completed_task_ids: list[int] | None = None,
        failed_task_ids: list[int] | None = None,
        partial_task_ids: list[int] | None = None,
        notes: list[str] | None = None,
        key_risks: list[str] | None = None,
    ) -> StageEvaluationOutput:
        return StageEvaluationOutput(
            decision=decision,
            decision_summary=decision_summary,
            stage_goals_satisfied=stage_goals_satisfied,
            project_stage_closed=project_stage_closed,
            recovery_strategy=recovery_strategy,
            recovery_reason=recovery_reason,
            replan=EvaluationReplanInstruction(
                required=replan_required,
                level=replan_level,
                reason=replan_reason,
                target_task_ids=[],
            ),
            followup_atomic_tasks_required=followup_atomic_tasks_required,
            followup_atomic_tasks_reason=followup_atomic_tasks_reason,
            manual_review_required=manual_review_required,
            manual_review_reason=manual_review_reason,
            recommended_next_action=recommended_next_action,
            recommended_next_action_reason=recommended_next_action_reason,
            evaluated_batches=[
                EvaluatedBatchSummary(
                    batch_id=evaluated_batch_id,
                    outcome=evaluated_outcome,
                    summary="Batch evaluation summary is sufficiently detailed.",
                    key_findings=["The evaluated batch produced coherent artifacts."],
                    failed_task_ids=failed_task_ids or [],
                    partial_task_ids=partial_task_ids or [],
                    completed_task_ids=completed_task_ids or [],
                )
            ],
            key_risks=key_risks or ["Remaining work still exists in later batches."],
            notes=notes or ["Proceed with the next batch."],
        )

    return _make_stage_evaluation_output


@pytest.fixture()
def make_recovery_decision() -> Callable[..., RecoveryDecision]:
    def _make_recovery_decision(
        *,
        source_task_id: int,
        source_run_id: int,
        action: str = "reatomize",
        requires_manual_review: bool = False,
        still_blocks_progress: bool = True,
        created_tasks: list[dict[str, Any]] | None = None,
        retry_same_task: bool = False,
        reason: str = "The original task needs recovery handling.",
        covered_gap_summary: str = "Recovery will address the uncovered work.",
        execution_guidance: str | None = "Use the recovery guidance to continue.",
    ) -> RecoveryDecision:
        task_payloads = [
            RecoveryTaskCreate(**payload)
            for payload in (created_tasks or [])
        ]
        return RecoveryDecision(
            source_task_id=source_task_id,
            source_run_id=source_run_id,
            action=action,
            confidence="high",
            reason=reason,
            covered_gap_summary=covered_gap_summary,
            execution_guidance=execution_guidance,
            retry_same_task=retry_same_task,
            requires_manual_review=requires_manual_review,
            still_blocks_progress=still_blocks_progress,
            created_tasks=task_payloads,
            decision_origin="post_batch_recovery",
        )

    return _make_recovery_decision


@pytest.fixture()
def make_execution_plan() -> Callable[..., ExecutionPlan]:
    def _make_execution_plan(
        *,
        batches: list[dict[str, Any]],
        plan_version: int = 1,
        global_goal: str = "Execute the current project stage successfully.",
        planning_scope: str = "project_atomic_tasks",
        sequencing_rationale: str = "The batches are ordered by dependency.",
        blocked_task_ids: list[int] | None = None,
        ready_task_ids: list[int] | None = None,
    ) -> ExecutionPlan:
        execution_batches: list[ExecutionBatch] = []
        checkpoints: list[CheckpointDefinition] = []

        total_batches = len(batches)

        for index, batch_data in enumerate(batches, start=1):
            batch_id = batch_data["batch_id"]
            checkpoint_id = batch_data.get("checkpoint_id", f"cp_{index}")

            evaluation_focus = batch_data.get("evaluation_focus")
            if evaluation_focus is None:
                evaluation_focus = ["functional_coverage"]
                if index == total_batches:
                    evaluation_focus = ["functional_coverage", "stage_closure"]

            checkpoint_reason = batch_data.get(
                "checkpoint_reason",
                f"Checkpoint after {batch_id}.",
            )

            execution_batches.append(
                ExecutionBatch(
                    batch_id=batch_id,
                    name=batch_data.get("name", f"Batch {index}"),
                    goal=batch_data.get("goal", f"Goal for {batch_id}"),
                    task_ids=batch_data["task_ids"],
                    entry_conditions=batch_data.get(
                        "entry_conditions",
                        ["Prior dependencies resolved."],
                    ),
                    expected_outputs=batch_data.get(
                        "expected_outputs",
                        ["Expected output generated."],
                    ),
                    risk_level=batch_data.get("risk_level", "medium"),
                    checkpoint_after=True,
                    checkpoint_id=checkpoint_id,
                    checkpoint_reason=checkpoint_reason,
                )
            )

            checkpoints.append(
                CheckpointDefinition(
                    checkpoint_id=checkpoint_id,
                    name=batch_data.get("checkpoint_name", f"Checkpoint {index}"),
                    reason=checkpoint_reason,
                    after_batch_id=batch_id,
                    evaluation_goal=batch_data.get(
                        "evaluation_goal",
                        f"Evaluate whether {batch_id} achieved its intended goal.",
                    ),
                    evaluation_focus=evaluation_focus,
                    can_introduce_new_tasks=True,
                    can_resequence_remaining_work=True,
                )
            )

        return ExecutionPlan(
            plan_version=plan_version,
            planning_scope=planning_scope,
            global_goal=global_goal,
            execution_batches=execution_batches,
            checkpoints=checkpoints,
            ready_task_ids=ready_task_ids or [],
            blocked_task_ids=blocked_task_ids or [],
            inferred_dependencies=[],
            sequencing_rationale=sequencing_rationale,
            uncertainties=[],
        )

    return _make_execution_plan