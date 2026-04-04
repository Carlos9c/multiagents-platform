from __future__ import annotations

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


_FINAL_STAGE_CLOSURE_FOCUS = "stage_closure"


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
    return f"checkpoint_plan_{plan_version}_batch_{anchor_batch_index}_patch_{patch_index}"


def _build_patch_batch_name(
    *,
    plan_version: int,
    anchor_batch_index: int,
    patch_index: int,
) -> str:
    return f"Plan {plan_version} · Batch {anchor_batch_index}.{patch_index}"


def _get_batch_or_raise(plan: ExecutionPlan, batch_id: str) -> ExecutionBatch:
    batch = next((batch for batch in plan.execution_batches if batch.batch_id == batch_id), None)
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


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _checkpoint_lookup(plan: ExecutionPlan) -> dict[str, CheckpointDefinition]:
    lookup: dict[str, CheckpointDefinition] = {}
    for checkpoint in plan.checkpoints:
        lookup[checkpoint.after_batch_id] = checkpoint
    return lookup


def _reindex_batches(
    *,
    batches: list[ExecutionBatch],
    plan_version: int,
) -> list[ExecutionBatch]:
    reindexed: list[ExecutionBatch] = []
    for position, batch in enumerate(batches, start=1):
        reindexed.append(
            batch.model_copy(
                update={
                    "batch_index": position,
                    "plan_version": plan_version,
                }
            )
        )
    return reindexed


def _normalize_checkpoint_for_batch(
    *,
    batch: ExecutionBatch,
    checkpoint: CheckpointDefinition,
    is_final_batch: bool,
) -> CheckpointDefinition:
    evaluation_focus = list(checkpoint.evaluation_focus or [])
    evaluation_focus = [str(item) for item in evaluation_focus if item]

    if is_final_batch:
        if _FINAL_STAGE_CLOSURE_FOCUS not in evaluation_focus:
            evaluation_focus.append(_FINAL_STAGE_CLOSURE_FOCUS)
    else:
        evaluation_focus = [item for item in evaluation_focus if item != _FINAL_STAGE_CLOSURE_FOCUS]

    evaluation_focus = _dedupe_preserve_order(evaluation_focus)

    return checkpoint.model_copy(
        update={
            "after_batch_id": batch.batch_id,
            "evaluation_focus": evaluation_focus,
        }
    )


def _order_batches_by_logical_execution_sequence(
    *,
    batches: list[ExecutionBatch],
) -> list[ExecutionBatch]:
    def _sort_key(batch: ExecutionBatch) -> tuple[int, int, int]:
        if batch.is_patch_batch:
            if batch.anchor_batch_index is None or batch.patch_index is None:
                raise ExecutionPlanPatchServiceError(
                    f"Patch batch '{batch.batch_id}' is missing anchor_batch_index or patch_index."
                )
            return (batch.anchor_batch_index, 1, batch.patch_index)

        return (batch.batch_index, 0, 0)

    ordered = sorted(batches, key=_sort_key)

    non_patch_batches = [batch for batch in ordered if not batch.is_patch_batch]
    non_patch_indexes = [batch.batch_index for batch in non_patch_batches]
    if non_patch_indexes != sorted(non_patch_indexes):
        raise ExecutionPlanPatchServiceError(
            "Execution plan contains non-patch batches that cannot be ordered consistently."
        )

    return ordered


def normalize_execution_plan_terminal_invariants(
    *,
    plan: ExecutionPlan,
) -> ExecutionPlan:
    if not plan.execution_batches:
        raise ExecutionPlanPatchServiceError(
            f"Execution plan version {plan.plan_version} has no execution batches."
        )

    checkpoint_by_after_batch_id = _checkpoint_lookup(plan)

    ordered_batches = _order_batches_by_logical_execution_sequence(
        batches=list(plan.execution_batches),
    )

    reindexed_batches = _reindex_batches(
        batches=ordered_batches,
        plan_version=plan.plan_version,
    )

    normalized_checkpoints: list[CheckpointDefinition] = []
    seen_checkpoint_ids: set[str] = set()

    for index, batch in enumerate(reindexed_batches):
        checkpoint = checkpoint_by_after_batch_id.get(batch.batch_id)
        if checkpoint is None:
            raise ExecutionPlanPatchServiceError(
                f"Batch '{batch.batch_id}' in plan version {plan.plan_version} has no checkpoint."
            )

        normalized_checkpoint = _normalize_checkpoint_for_batch(
            batch=batch,
            checkpoint=checkpoint,
            is_final_batch=index == len(reindexed_batches) - 1,
        )

        if normalized_checkpoint.checkpoint_id in seen_checkpoint_ids:
            raise ExecutionPlanPatchServiceError(
                f"Duplicate checkpoint_id '{normalized_checkpoint.checkpoint_id}' detected "
                f"in execution plan version {plan.plan_version}."
            )

        seen_checkpoint_ids.add(normalized_checkpoint.checkpoint_id)
        normalized_checkpoints.append(normalized_checkpoint)

    if len(normalized_checkpoints) != len(reindexed_batches):
        raise ExecutionPlanPatchServiceError(
            f"Execution plan version {plan.plan_version} must contain exactly one checkpoint "
            "per execution batch after normalization."
        )

    final_checkpoint = normalized_checkpoints[-1]
    if _FINAL_STAGE_CLOSURE_FOCUS not in final_checkpoint.evaluation_focus:
        raise ExecutionPlanPatchServiceError(
            f"Execution plan version {plan.plan_version} is invalid after normalization: "
            f"the final checkpoint '{final_checkpoint.checkpoint_id}' for batch "
            f"'{reindexed_batches[-1].batch_id}' does not include '{_FINAL_STAGE_CLOSURE_FOCUS}'."
        )

    return ExecutionPlan(
        plan_version=plan.plan_version,
        supersedes_plan_version=plan.supersedes_plan_version,
        planning_scope=plan.planning_scope,
        global_goal=plan.global_goal,
        execution_batches=reindexed_batches,
        checkpoints=normalized_checkpoints,
        ready_task_ids=list(plan.ready_task_ids),
        blocked_task_ids=list(plan.blocked_task_ids),
        inferred_dependencies=list(plan.inferred_dependencies),
        sequencing_rationale=plan.sequencing_rationale,
        uncertainties=list(plan.uncertainties),
    )


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
        raise ExecutionPlanPatchServiceError("Patch batch insertion requires at least one task_id.")

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
        batch_index=anchor_batch_index + 1,
        plan_version=plan.plan_version,
        name=_build_patch_batch_name(
            plan_version=plan.plan_version,
            anchor_batch_index=anchor_batch_index,
            patch_index=patch_index,
        ),
        goal=goal,
        task_ids=list(task_ids),
        entry_conditions=list(entry_conditions or ["Patch batch inserted after checkpoint."]),
        expected_outputs=list(expected_outputs or ["Patch work completed and validated."]),
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
        and plan.execution_batches[insert_position].anchor_batch_index == anchor_batch_index
    ):
        insert_position += 1

    patched_batches = list(plan.execution_batches)
    patched_batches.insert(insert_position, patch_batch)

    patched_checkpoints = list(plan.checkpoints)
    patched_checkpoints.append(patch_checkpoint)

    provisional_plan = ExecutionPlan(
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

    return normalize_execution_plan_terminal_invariants(plan=provisional_plan)


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
