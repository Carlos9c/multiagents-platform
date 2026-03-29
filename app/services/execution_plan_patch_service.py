import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.schemas.execution_plan import (
    CheckpointDefinition,
    ExecutionBatch,
    ExecutionPlan,
)
from app.services.artifacts import create_artifact


class ExecutionPlanPatchServiceError(Exception):
    """Raised when a local patch batch cannot be inserted into the current plan."""


def _build_patch_batch_internal_id(
    *,
    plan_version: int,
    anchor_batch_index: int,
    patch_index: int,
) -> str:
    return f"{plan_version}_{anchor_batch_index}_p{patch_index}"


def _build_patch_batch_id(
    *,
    plan_version: int,
    anchor_batch_index: int,
    patch_index: int,
) -> str:
    return f"plan_{plan_version}_batch_{anchor_batch_index}_patch_{patch_index}"


def _build_patch_checkpoint_id(
    *,
    plan_version: int,
    anchor_batch_index: int,
    patch_index: int,
) -> str:
    return (
        f"checkpoint_plan_{plan_version}_batch_{anchor_batch_index}_patch_{patch_index}"
    )


def _build_patch_batch_name(
    *,
    plan_version: int,
    anchor_batch_index: int,
    patch_index: int,
) -> str:
    return f"Plan {plan_version} · Batch {anchor_batch_index}.{patch_index}"


def _get_batch_or_raise(plan: ExecutionPlan, batch_id: str) -> ExecutionBatch:
    batch = next(
        (batch for batch in plan.execution_batches if batch.batch_id == batch_id), None
    )
    if batch is None:
        raise ExecutionPlanPatchServiceError(
            f"Batch '{batch_id}' not found in execution plan version {plan.plan_version}."
        )
    return batch


def _next_patch_index_for_anchor(
    *,
    plan: ExecutionPlan,
    anchor_batch_index: int,
) -> int:
    patch_indexes = [
        batch.patch_index
        for batch in plan.execution_batches
        if batch.is_patch_batch and batch.anchor_batch_index == anchor_batch_index
    ]
    patch_indexes = [value for value in patch_indexes if value is not None]
    return (max(patch_indexes) if patch_indexes else 0) + 1


def insert_patch_batch_after_batch(
    *,
    plan: ExecutionPlan,
    anchor_batch_id: str,
    task_ids: list[int],
    goal: str,
    checkpoint_reason: str,
    entry_conditions: list[str] | None = None,
    expected_outputs: list[str] | None = None,
    risk_level: str = "medium",
) -> ExecutionPlan:
    if not task_ids:
        raise ExecutionPlanPatchServiceError(
            "Patch batch insertion requires at least one task_id."
        )

    anchor_batch = _get_batch_or_raise(plan, anchor_batch_id)
    anchor_batch_index = anchor_batch.batch_index
    patch_index = _next_patch_index_for_anchor(
        plan=plan,
        anchor_batch_index=anchor_batch_index,
    )

    patch_batch_internal_id = _build_patch_batch_internal_id(
        plan_version=plan.plan_version,
        anchor_batch_index=anchor_batch_index,
        patch_index=patch_index,
    )
    patch_batch_id = _build_patch_batch_id(
        plan_version=plan.plan_version,
        anchor_batch_index=anchor_batch_index,
        patch_index=patch_index,
    )
    checkpoint_id = _build_patch_checkpoint_id(
        plan_version=plan.plan_version,
        anchor_batch_index=anchor_batch_index,
        patch_index=patch_index,
    )

    patch_batch = ExecutionBatch(
        batch_internal_id=patch_batch_internal_id,
        batch_id=patch_batch_id,
        batch_index=anchor_batch_index,
        plan_version=plan.plan_version,
        name=_build_patch_batch_name(
            plan_version=plan.plan_version,
            anchor_batch_index=anchor_batch_index,
            patch_index=patch_index,
        ),
        goal=goal,
        task_ids=list(task_ids),
        entry_conditions=list(
            entry_conditions or ["Patch batch inserted after checkpoint."]
        ),
        expected_outputs=list(
            expected_outputs or ["Patch work completed and validated."]
        ),
        risk_level=risk_level,
        checkpoint_after=True,
        checkpoint_id=checkpoint_id,
        checkpoint_reason=checkpoint_reason,
        is_patch_batch=True,
        anchor_batch_index=anchor_batch_index,
        patch_index=patch_index,
    )

    patch_checkpoint = CheckpointDefinition(
        checkpoint_id=checkpoint_id,
        name=f"Patch checkpoint {anchor_batch_index}.{patch_index}",
        reason=checkpoint_reason,
        after_batch_id=patch_batch_id,
        evaluation_goal=(
            "Evaluate whether the inserted patch batch resolved the new recovery work "
            "required before continuing the remaining plan."
        ),
        evaluation_focus=["functional_coverage", "dependency_validation"],
        can_introduce_new_tasks=True,
        can_resequence_remaining_work=True,
    )

    anchor_position = next(
        index
        for index, batch in enumerate(plan.execution_batches)
        if batch.batch_id == anchor_batch_id
    )

    insert_position = anchor_position + 1
    while (
        insert_position < len(plan.execution_batches)
        and plan.execution_batches[insert_position].is_patch_batch
        and plan.execution_batches[insert_position].anchor_batch_index
        == anchor_batch_index
    ):
        insert_position += 1

    patched_batches = list(plan.execution_batches)
    patched_batches.insert(insert_position, patch_batch)

    patched_checkpoints = list(plan.checkpoints)
    patched_checkpoints.append(patch_checkpoint)

    return ExecutionPlan(
        plan_version=plan.plan_version,
        supersedes_plan_version=plan.supersedes_plan_version,
        planning_scope=plan.planning_scope,
        global_goal=plan.global_goal,
        execution_batches=patched_batches,
        checkpoints=patched_checkpoints,
        ready_task_ids=list(plan.ready_task_ids),
        blocked_task_ids=list(plan.blocked_task_ids),
        inferred_dependencies=list(plan.inferred_dependencies),
        sequencing_rationale=plan.sequencing_rationale,
        uncertainties=list(plan.uncertainties),
    )


def persist_patched_execution_plan(
    db: Session,
    *,
    project_id: int,
    plan: ExecutionPlan,
    created_by: str = "execution_plan_patch_service",
) -> Artifact:
    content = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2)
    return create_artifact(
        db=db,
        project_id=project_id,
        task_id=None,
        artifact_type="execution_plan_patch",
        content=content,
        created_by=created_by,
    )
