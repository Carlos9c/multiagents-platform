import pytest

from app.schemas.execution_plan import CheckpointDefinition, ExecutionBatch, ExecutionPlan
from app.services.execution_plan_patch_service import (
    ExecutionPlanPatchServiceError,
    insert_patch_batch_after_batch,
    normalize_execution_plan_terminal_invariants,
)


def test_insert_patch_batch_after_batch_creates_plan_local_patch(make_execution_plan):
    plan = make_execution_plan(
        plan_version=3,
        supersedes_plan_version=2,
        batches=[
            {
                "batch_id": "plan_3_batch_1",
                "batch_internal_id": "3_1",
                "batch_index": 1,
                "plan_version": 3,
                "task_ids": [10],
                "checkpoint_id": "checkpoint_plan_3_batch_1",
                "checkpoint_reason": "Validate batch 1.",
            },
            {
                "batch_id": "plan_3_batch_2",
                "batch_internal_id": "3_2",
                "batch_index": 2,
                "plan_version": 3,
                "task_ids": [20],
                "checkpoint_id": "checkpoint_plan_3_batch_2",
                "checkpoint_reason": "Validate batch 2.",
            },
        ],
    )

    patched = insert_patch_batch_after_batch(
        plan=plan,
        anchor_batch_id="plan_3_batch_1",
        task_ids=[101, 102],
        goal="Execute local recovery patch before continuing.",
        checkpoint_reason="Validate patch batch.",
    )

    assert patched.plan_version == 3
    assert patched.supersedes_plan_version == 2
    assert len(patched.execution_batches) == 3
    assert len(patched.checkpoints) == 3

    first_batch = patched.execution_batches[0]
    patch_batch = patched.execution_batches[1]
    trailing_batch = patched.execution_batches[2]

    assert first_batch.batch_internal_id == "3_1"
    assert first_batch.batch_id == "plan_3_batch_1"

    assert patch_batch.is_patch_batch is True
    assert patch_batch.anchor_batch_index == 1
    assert patch_batch.patch_index == 1
    assert patch_batch.plan_version == 3
    assert patch_batch.batch_internal_id == "3_1_p1"
    assert patch_batch.batch_id == "plan_3_batch_1_patch_1"
    assert patch_batch.name == "Plan 3 · Batch 1.1"
    assert patch_batch.task_ids == [101, 102]
    assert patch_batch.goal == "Execute local recovery patch before continuing."
    assert patch_batch.checkpoint_reason == "Validate patch batch."

    assert trailing_batch.batch_internal_id == "3_2"
    assert trailing_batch.batch_id == "plan_3_batch_2"
    assert trailing_batch.task_ids == [20]

    checkpoint_by_id = {checkpoint.checkpoint_id: checkpoint for checkpoint in patched.checkpoints}

    assert patch_batch.checkpoint_id in checkpoint_by_id
    patch_checkpoint = checkpoint_by_id[patch_batch.checkpoint_id]

    assert patch_checkpoint.after_batch_id == patch_batch.batch_id
    assert patch_checkpoint.reason == "Validate patch batch."
    assert patch_checkpoint.can_introduce_new_tasks is True
    assert patch_checkpoint.can_resequence_remaining_work is True


def _make_plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_version=1,
        supersedes_plan_version=None,
        planning_scope="project_atomic_tasks",
        global_goal="Test plan",
        execution_batches=[
            ExecutionBatch(
                batch_internal_id="1_1",
                batch_id="plan_1_batch_1",
                batch_index=1,
                plan_version=1,
                name="Batch 1",
                goal="First batch",
                task_ids=[101],
                entry_conditions=["ready"],
                expected_outputs=["done"],
                risk_level="low",
                checkpoint_after=True,
                checkpoint_id="checkpoint_plan_1_batch_1",
                checkpoint_reason="Check batch 1",
                is_patch_batch=False,
                anchor_batch_index=None,
                patch_index=None,
            ),
            ExecutionBatch(
                batch_internal_id="1_2",
                batch_id="plan_1_batch_2",
                batch_index=2,
                plan_version=1,
                name="Batch 2",
                goal="Second batch",
                task_ids=[102],
                entry_conditions=["ready"],
                expected_outputs=["done"],
                risk_level="medium",
                checkpoint_after=True,
                checkpoint_id="checkpoint_plan_1_batch_2",
                checkpoint_reason="Check batch 2",
                is_patch_batch=False,
                anchor_batch_index=None,
                patch_index=None,
            ),
            ExecutionBatch(
                batch_internal_id="1_3",
                batch_id="plan_1_batch_3",
                batch_index=3,
                plan_version=1,
                name="Batch 3",
                goal="Final batch",
                task_ids=[103],
                entry_conditions=["ready"],
                expected_outputs=["done"],
                risk_level="medium",
                checkpoint_after=True,
                checkpoint_id="checkpoint_plan_1_batch_3",
                checkpoint_reason="Check batch 3",
                is_patch_batch=False,
                anchor_batch_index=None,
                patch_index=None,
            ),
        ],
        checkpoints=[
            CheckpointDefinition(
                checkpoint_id="checkpoint_plan_1_batch_1",
                name="Checkpoint 1",
                reason="Check batch 1",
                after_batch_id="plan_1_batch_1",
                evaluation_goal="Evaluate batch 1",
                evaluation_focus=["functional_coverage"],
                can_introduce_new_tasks=True,
                can_resequence_remaining_work=True,
            ),
            CheckpointDefinition(
                checkpoint_id="checkpoint_plan_1_batch_2",
                name="Checkpoint 2",
                reason="Check batch 2",
                after_batch_id="plan_1_batch_2",
                evaluation_goal="Evaluate batch 2",
                evaluation_focus=["functional_coverage", "dependency_validation"],
                can_introduce_new_tasks=True,
                can_resequence_remaining_work=True,
            ),
            CheckpointDefinition(
                checkpoint_id="checkpoint_plan_1_batch_3",
                name="Checkpoint 3",
                reason="Check batch 3",
                after_batch_id="plan_1_batch_3",
                evaluation_goal="Evaluate batch 3",
                evaluation_focus=["functional_coverage", "stage_closure"],
                can_introduce_new_tasks=True,
                can_resequence_remaining_work=True,
            ),
        ],
        ready_task_ids=[101, 102, 103],
        blocked_task_ids=[],
        inferred_dependencies=[],
        sequencing_rationale="Simple ordered plan",
        uncertainties=[],
    )


def test_insert_patch_batch_after_intermediate_batch_preserves_final_stage_closure():
    plan = _make_plan()

    patched_plan = insert_patch_batch_after_batch(
        plan=plan,
        anchor_batch_id="plan_1_batch_1",
        task_ids=[201, 202],
        goal="Execute recovery work before continuing.",
        checkpoint_reason="Validate the patch batch before continuing.",
    )

    assert len(patched_plan.execution_batches) == 4
    assert [batch.batch_index for batch in patched_plan.execution_batches] == [1, 2, 3, 4]

    assert patched_plan.execution_batches[0].batch_id == "plan_1_batch_1"
    assert patched_plan.execution_batches[1].is_patch_batch is True
    assert patched_plan.execution_batches[1].anchor_batch_index == 1
    assert patched_plan.execution_batches[2].batch_id == "plan_1_batch_2"
    assert patched_plan.execution_batches[3].batch_id == "plan_1_batch_3"

    checkpoints_by_batch = {
        checkpoint.after_batch_id: checkpoint for checkpoint in patched_plan.checkpoints
    }

    assert "stage_closure" not in checkpoints_by_batch["plan_1_batch_1"].evaluation_focus
    assert (
        "stage_closure"
        not in checkpoints_by_batch[patched_plan.execution_batches[1].batch_id].evaluation_focus
    )
    assert "stage_closure" not in checkpoints_by_batch["plan_1_batch_2"].evaluation_focus
    assert "stage_closure" in checkpoints_by_batch["plan_1_batch_3"].evaluation_focus


def test_insert_patch_batch_after_penultimate_batch_keeps_single_valid_final_closure():
    plan = _make_plan()

    patched_plan = insert_patch_batch_after_batch(
        plan=plan,
        anchor_batch_id="plan_1_batch_2",
        task_ids=[301],
        goal="Execute blocking recovery before the final batch.",
        checkpoint_reason="Validate the patch batch before the final batch.",
    )

    assert len(patched_plan.execution_batches) == 4
    assert [batch.batch_index for batch in patched_plan.execution_batches] == [1, 2, 3, 4]

    assert patched_plan.execution_batches[0].batch_id == "plan_1_batch_1"
    assert patched_plan.execution_batches[1].batch_id == "plan_1_batch_2"
    assert patched_plan.execution_batches[2].is_patch_batch is True
    assert patched_plan.execution_batches[2].anchor_batch_index == 2
    assert patched_plan.execution_batches[3].batch_id == "plan_1_batch_3"

    checkpoints_with_stage_closure = [
        checkpoint
        for checkpoint in patched_plan.checkpoints
        if "stage_closure" in checkpoint.evaluation_focus
    ]

    assert len(checkpoints_with_stage_closure) == 1
    assert checkpoints_with_stage_closure[0].after_batch_id == "plan_1_batch_3"


def test_normalize_execution_plan_terminal_invariants_moves_stage_closure_to_real_final_batch():
    plan = _make_plan()

    patch_batch = ExecutionBatch(
        batch_internal_id="1_2_p1",
        batch_id="plan_1_batch_2_patch_1",
        batch_index=2,
        plan_version=1,
        name="Patch 2.1",
        goal="Patch batch",
        task_ids=[401],
        entry_conditions=["ready"],
        expected_outputs=["done"],
        risk_level="medium",
        checkpoint_after=True,
        checkpoint_id="checkpoint_plan_1_batch_2_patch_1",
        checkpoint_reason="Check patch",
        is_patch_batch=True,
        anchor_batch_index=2,
        patch_index=1,
    )

    broken_plan = ExecutionPlan.model_construct(
        plan_version=plan.plan_version,
        supersedes_plan_version=plan.supersedes_plan_version,
        planning_scope=plan.planning_scope,
        global_goal=plan.global_goal,
        execution_batches=[
            plan.execution_batches[0],
            plan.execution_batches[1],
            plan.execution_batches[2],
            patch_batch,
        ],
        checkpoints=[
            plan.checkpoints[0],
            plan.checkpoints[1],
            plan.checkpoints[2],
            CheckpointDefinition(
                checkpoint_id="checkpoint_plan_1_batch_2_patch_1",
                name="Patch checkpoint 2.1",
                reason="Check patch",
                after_batch_id="plan_1_batch_2_patch_1",
                evaluation_goal="Evaluate patch",
                evaluation_focus=["functional_coverage"],
                can_introduce_new_tasks=True,
                can_resequence_remaining_work=True,
            ),
        ],
        ready_task_ids=list(plan.ready_task_ids),
        blocked_task_ids=list(plan.blocked_task_ids),
        inferred_dependencies=list(plan.inferred_dependencies),
        sequencing_rationale=plan.sequencing_rationale,
        uncertainties=list(plan.uncertainties),
    )

    normalized_plan = normalize_execution_plan_terminal_invariants(plan=broken_plan)

    assert [batch.batch_id for batch in normalized_plan.execution_batches] == [
        "plan_1_batch_1",
        "plan_1_batch_2",
        "plan_1_batch_2_patch_1",
        "plan_1_batch_3",
    ]

    final_batch = normalized_plan.execution_batches[-1]
    checkpoints_by_batch = {
        checkpoint.after_batch_id: checkpoint for checkpoint in normalized_plan.checkpoints
    }

    assert final_batch.batch_id == "plan_1_batch_3"
    assert "stage_closure" in checkpoints_by_batch["plan_1_batch_3"].evaluation_focus
    assert "stage_closure" not in checkpoints_by_batch["plan_1_batch_2_patch_1"].evaluation_focus


def test_normalize_execution_plan_terminal_invariants_removes_stage_closure_from_non_final_checkpoints():
    plan = _make_plan()

    broken_plan = ExecutionPlan(
        plan_version=plan.plan_version,
        supersedes_plan_version=plan.supersedes_plan_version,
        planning_scope=plan.planning_scope,
        global_goal=plan.global_goal,
        execution_batches=list(plan.execution_batches),
        checkpoints=[
            CheckpointDefinition(
                checkpoint_id="checkpoint_plan_1_batch_1",
                name="Checkpoint 1",
                reason="Check batch 1",
                after_batch_id="plan_1_batch_1",
                evaluation_goal="Evaluate batch 1",
                evaluation_focus=["functional_coverage", "stage_closure"],
                can_introduce_new_tasks=True,
                can_resequence_remaining_work=True,
            ),
            plan.checkpoints[1],
            plan.checkpoints[2],
        ],
        ready_task_ids=list(plan.ready_task_ids),
        blocked_task_ids=list(plan.blocked_task_ids),
        inferred_dependencies=list(plan.inferred_dependencies),
        sequencing_rationale=plan.sequencing_rationale,
        uncertainties=list(plan.uncertainties),
    )

    normalized_plan = normalize_execution_plan_terminal_invariants(plan=broken_plan)

    checkpoints_by_batch = {
        checkpoint.after_batch_id: checkpoint for checkpoint in normalized_plan.checkpoints
    }

    assert "stage_closure" not in checkpoints_by_batch["plan_1_batch_1"].evaluation_focus
    assert "stage_closure" not in checkpoints_by_batch["plan_1_batch_2"].evaluation_focus
    assert "stage_closure" in checkpoints_by_batch["plan_1_batch_3"].evaluation_focus


def test_normalize_execution_plan_terminal_invariants_requires_one_checkpoint_per_batch():
    plan = _make_plan()

    broken_plan = ExecutionPlan.model_construct(
        plan_version=plan.plan_version,
        supersedes_plan_version=plan.supersedes_plan_version,
        planning_scope=plan.planning_scope,
        global_goal=plan.global_goal,
        execution_batches=list(plan.execution_batches),
        checkpoints=[
            plan.checkpoints[0],
            plan.checkpoints[2],
        ],
        ready_task_ids=list(plan.ready_task_ids),
        blocked_task_ids=list(plan.blocked_task_ids),
        inferred_dependencies=list(plan.inferred_dependencies),
        sequencing_rationale=plan.sequencing_rationale,
        uncertainties=list(plan.uncertainties),
    )

    with pytest.raises(
        ExecutionPlanPatchServiceError,
        match="has no checkpoint",
    ):
        normalize_execution_plan_terminal_invariants(plan=broken_plan)
