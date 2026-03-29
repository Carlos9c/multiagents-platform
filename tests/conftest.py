# ruff: noqa: E402
import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault(
    "AGENTS_PROJECTS_ROOT", str(Path.cwd() / ".pytest_agents_projects")
)

from app.db.base import Base
from app.models.artifact import Artifact
from app.models.execution_run import (
    EXECUTION_RUN_STATUS_SUCCEEDED,
    ExecutionRun,
)
from app.models.project import Project
from app.models.task import (
    EXECUTION_ENGINE,
    PLANNING_LEVEL_ATOMIC,
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
        enable_technical_refinement: bool = False,
        plan_version: int = 1,
    ) -> Project:
        project = Project(
            name=name,
            description=description,
            enable_technical_refinement=enable_technical_refinement,
            plan_version=plan_version,
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
        stage_goals_satisfied: bool = False,
        project_stage_closed: bool = False,
        recovery_strategy: str = "none",
        recovery_reason: str | None = None,
        replan_required: bool = False,
        replan_level: str | None = None,
        replan_reason: str | None = None,
        replan_target_task_ids: list[int] | None = None,
        followup_atomic_tasks_required: bool = False,
        followup_atomic_tasks_reason: str | None = None,
        manual_review_required: bool = False,
        manual_review_reason: str | None = None,
        recommended_next_action: str | None = None,
        recommended_next_action_reason: str | None = None,
        decision_signals: list[str] | None = None,
        plan_change_scope: str = "none",
        remaining_plan_still_valid: bool = True,
        new_recovery_tasks_blocking: bool | None = None,
        single_task_tail_risk: bool = False,
        evaluated_batches: list[EvaluatedBatchSummary] | None = None,
        key_risks: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> StageEvaluationOutput:
        replan_instruction = EvaluationReplanInstruction(
            required=replan_required,
            level=replan_level,
            reason=replan_reason,
            target_task_ids=list(replan_target_task_ids or []),
        )

        return StageEvaluationOutput(
            decision=decision,
            decision_summary=decision_summary,
            stage_goals_satisfied=stage_goals_satisfied,
            project_stage_closed=project_stage_closed,
            recovery_strategy=recovery_strategy,
            recovery_reason=recovery_reason,
            replan=replan_instruction,
            followup_atomic_tasks_required=followup_atomic_tasks_required,
            followup_atomic_tasks_reason=followup_atomic_tasks_reason,
            manual_review_required=manual_review_required,
            manual_review_reason=manual_review_reason,
            recommended_next_action=recommended_next_action,
            recommended_next_action_reason=recommended_next_action_reason,
            decision_signals=list(decision_signals or []),
            plan_change_scope=plan_change_scope,
            remaining_plan_still_valid=remaining_plan_still_valid,
            new_recovery_tasks_blocking=new_recovery_tasks_blocking,
            single_task_tail_risk=single_task_tail_risk,
            evaluated_batches=list(evaluated_batches or []),
            key_risks=list(key_risks or []),
            notes=list(notes or []),
        )

    return _make_stage_evaluation_output


@pytest.fixture()
def make_recovery_decision() -> Callable[..., RecoveryDecision]:
    def _make_recovery_decision(
        *,
        source_task_id: int,
        source_run_id: int,
        action: str = "reatomize",
        confidence: str = "high",
        requires_manual_review: bool = False,
        still_blocks_progress: bool = True,
        created_tasks: list[dict[str, Any]] | None = None,
        reason: str = "The original task needs recovery handling.",
        covered_gap_summary: str = "Recovery will address the uncovered work.",
        execution_guidance: str | None = "Use the recovery guidance to continue.",
        evaluation_guidance: str | None = None,
        decision_origin: str | None = "post_batch_recovery",
    ) -> RecoveryDecision:
        task_payloads = [
            RecoveryTaskCreate(**payload) for payload in (created_tasks or [])
        ]
        return RecoveryDecision(
            source_task_id=source_task_id,
            source_run_id=source_run_id,
            action=action,
            confidence=confidence,
            reason=reason,
            covered_gap_summary=covered_gap_summary,
            execution_guidance=execution_guidance,
            evaluation_guidance=evaluation_guidance,
            requires_manual_review=requires_manual_review,
            still_blocks_progress=still_blocks_progress,
            created_tasks=task_payloads,
            decision_origin=decision_origin,
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
        supersedes_plan_version: int | None = None,
    ) -> ExecutionPlan:
        execution_batches: list[ExecutionBatch] = []
        checkpoints: list[CheckpointDefinition] = []

        total_batches = len(batches)
        if total_batches == 0:
            raise ValueError("make_execution_plan requires at least one batch.")

        if supersedes_plan_version is None and plan_version > 1:
            supersedes_plan_version = plan_version - 1

        for index, batch_data in enumerate(batches, start=1):
            effective_plan_version = batch_data.get("plan_version", plan_version)
            effective_batch_index = batch_data.get("batch_index", index)

            batch_id = batch_data.get(
                "batch_id",
                f"plan_{effective_plan_version}_batch_{effective_batch_index}",
            )
            checkpoint_id = batch_data.get(
                "checkpoint_id",
                f"checkpoint_plan_{effective_plan_version}_batch_{effective_batch_index}",
            )

            evaluation_focus = batch_data.get("evaluation_focus")
            if evaluation_focus is None:
                evaluation_focus = ["functional_coverage"]
                if index == total_batches:
                    evaluation_focus = ["functional_coverage", "stage_closure"]
            elif index == total_batches and "stage_closure" not in evaluation_focus:
                evaluation_focus = [*evaluation_focus, "stage_closure"]

            checkpoint_reason = batch_data.get(
                "checkpoint_reason",
                f"Checkpoint after {batch_id}.",
            )

            execution_batches.append(
                ExecutionBatch(
                    batch_internal_id=batch_data.get(
                        "batch_internal_id",
                        f"{effective_plan_version}_{effective_batch_index}",
                    ),
                    batch_id=batch_id,
                    batch_index=effective_batch_index,
                    plan_version=effective_plan_version,
                    name=batch_data.get(
                        "name",
                        f"Plan {effective_plan_version} · Batch {effective_batch_index}",
                    ),
                    goal=batch_data.get("goal", f"Goal for {batch_id}"),
                    task_ids=list(batch_data["task_ids"]),
                    entry_conditions=list(
                        batch_data.get(
                            "entry_conditions",
                            ["Prior dependencies resolved."],
                        )
                    ),
                    expected_outputs=list(
                        batch_data.get(
                            "expected_outputs",
                            ["Expected output generated."],
                        )
                    ),
                    risk_level=batch_data.get("risk_level", "medium"),
                    checkpoint_after=batch_data.get("checkpoint_after", True),
                    checkpoint_id=checkpoint_id,
                    checkpoint_reason=checkpoint_reason,
                    is_patch_batch=batch_data.get("is_patch_batch", False),
                    anchor_batch_index=batch_data.get("anchor_batch_index"),
                    patch_index=batch_data.get("patch_index"),
                )
            )

            checkpoints.append(
                CheckpointDefinition(
                    checkpoint_id=checkpoint_id,
                    name=batch_data.get(
                        "checkpoint_name",
                        f"Checkpoint {effective_batch_index}",
                    ),
                    reason=checkpoint_reason,
                    after_batch_id=batch_id,
                    evaluation_goal=batch_data.get(
                        "evaluation_goal",
                        f"Evaluate whether {batch_id} achieved its intended goal.",
                    ),
                    evaluation_focus=list(evaluation_focus),
                    can_introduce_new_tasks=batch_data.get(
                        "can_introduce_new_tasks",
                        True,
                    ),
                    can_resequence_remaining_work=batch_data.get(
                        "can_resequence_remaining_work",
                        True,
                    ),
                )
            )

        return ExecutionPlan(
            plan_version=plan_version,
            supersedes_plan_version=supersedes_plan_version,
            planning_scope=planning_scope,
            global_goal=global_goal,
            execution_batches=execution_batches,
            checkpoints=checkpoints,
            ready_task_ids=list(ready_task_ids or []),
            blocked_task_ids=list(blocked_task_ids or []),
            inferred_dependencies=[],
            sequencing_rationale=sequencing_rationale,
            uncertainties=[],
        )

    return _make_execution_plan
