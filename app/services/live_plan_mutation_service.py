from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models.project import Project
from app.schemas.execution_plan import ExecutionBatch, ExecutionPlan
from app.schemas.post_batch import PostBatchTaskRunSummary
from app.schemas.post_batch_intent import ResolvedPostBatchIntent
from app.schemas.recovery import RecoveryContext
from app.services.execution_plan_patch_service import (
    insert_patch_batch_after_batch,
    persist_patched_execution_plan,
)
from app.services.recovery_assignment_client import call_recovery_assignment_model
from app.services.recovery_assignment_compiler_service import (
    RecoveryAssignmentCompilerError,
    compile_recovery_assignment_plan,
)


LivePlanMutationKind = Literal[
    "none",
    "assignment",
    "resequence_patch",
    "resequence_deferred",
    "escalated_to_replan",
]


class LivePlanMutationServiceError(Exception):
    """Base exception for live plan mutation failures."""


@dataclass(frozen=True)
class LivePlanMutationResult:
    mutation_kind: LivePlanMutationKind
    patched_execution_plan: ExecutionPlan | None
    requires_replan: bool
    notes: list[str]
    metadata: dict[str, Any]


def _read_attr(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return bool(value)


def _should_run_immediate_resequence_patch(
    *,
    intent: ResolvedPostBatchIntent,
    created_recovery_task_ids: list[int],
    evaluation_decision: Any,
) -> bool:
    if intent.intent_type != "resequence":
        return False

    if intent.mutation_scope != "resequence":
        raise LivePlanMutationServiceError(
            "Resolved resequence intent must carry mutation_scope='resequence'."
        )

    if not created_recovery_task_ids:
        return False

    if not _normalize_bool(
        _read_attr(evaluation_decision, "new_recovery_tasks_blocking"),
        False,
    ):
        return False

    if not intent.remaining_plan_still_valid:
        raise LivePlanMutationServiceError(
            "Cannot apply an immediate resequence patch when the remaining plan is not valid."
        )

    return True


def mutate_live_plan(
    *,
    db: Session,
    project: Project,
    plan: ExecutionPlan,
    batch: ExecutionBatch,
    resolved_intent: ResolvedPostBatchIntent,
    evaluation_decision: Any,
    recovery_context: RecoveryContext,
    created_recovery_task_ids: list[int],
    executed_task_ids: list[int],
    successful_task_ids: list[int],
    problematic_run_ids: list[int],
    task_run_summaries: list[PostBatchTaskRunSummary],
    build_recovery_assignment_input_fn,
    persist_recovery_assignment_payload_fn,
) -> LivePlanMutationResult:
    if resolved_intent.intent_type == "assign":
        if resolved_intent.mutation_scope != "assignment":
            raise LivePlanMutationServiceError(
                "Resolved assign intent must carry mutation_scope='assignment'."
            )

        if not resolved_intent.requires_plan_mutation:
            raise LivePlanMutationServiceError("Resolved assign intent must require plan mutation.")

        if not resolved_intent.requires_all_new_tasks_assigned:
            raise LivePlanMutationServiceError(
                "Resolved assign intent must require all new tasks to be assigned."
            )

        if not created_recovery_task_ids:
            raise LivePlanMutationServiceError(
                "Resolved assign intent requires newly created recovery tasks, but none were found."
            )

        assignment_input = build_recovery_assignment_input_fn(
            db=db,
            project=project,
            plan=plan,
            batch=batch,
            evaluation_decision=evaluation_decision,
            recovery_context=recovery_context,
            created_recovery_task_ids=created_recovery_task_ids,
            executed_task_ids=executed_task_ids,
            successful_task_ids=successful_task_ids,
            problematic_run_ids=problematic_run_ids,
            task_run_summaries=task_run_summaries,
            resolved_intent_type=resolved_intent.intent_type,
            resolved_mutation_scope=resolved_intent.mutation_scope,
        )

        persist_recovery_assignment_payload_fn(
            db=db,
            project_id=project.id,
            artifact_type="recovery_assignment_input",
            payload=assignment_input.model_dump(mode="json"),
        )

        assignment_output = call_recovery_assignment_model(
            assignment_input=assignment_input,
        )

        persist_recovery_assignment_payload_fn(
            db=db,
            project_id=project.id,
            artifact_type="recovery_assignment_output",
            payload=assignment_output.model_dump(mode="json"),
        )

        try:
            compiled_assignment = compile_recovery_assignment_plan(
                plan=plan,
                assignment_input=assignment_input,
                assignment_output=assignment_output,
            )
        except RecoveryAssignmentCompilerError as exc:
            raise LivePlanMutationServiceError(
                f"Recovery assignment compilation failed: {str(exc)}"
            ) from exc

        if compiled_assignment.requires_replan:
            return LivePlanMutationResult(
                mutation_kind="escalated_to_replan",
                patched_execution_plan=None,
                requires_replan=True,
                notes=list(compiled_assignment.notes),
                metadata={
                    "assigned_task_ids": [],
                    "unassigned_task_ids": list(compiled_assignment.unassigned_task_ids),
                    "compiled_cluster_assignments": [],
                },
            )

        if compiled_assignment.patched_execution_plan is None:
            raise LivePlanMutationServiceError(
                "Recovery assignment completed without a patched execution plan."
            )

        patched_execution_plan = compiled_assignment.patched_execution_plan

        persist_patched_execution_plan(
            db=db,
            project_id=project.id,
            plan=patched_execution_plan,
            created_by="live_plan_mutation_service",
        )

        persist_recovery_assignment_payload_fn(
            db=db,
            project_id=project.id,
            artifact_type="recovery_assignment_compiled_plan",
            payload={
                "strategy": compiled_assignment.strategy,
                "requires_replan": compiled_assignment.requires_replan,
                "assigned_task_ids": compiled_assignment.assigned_task_ids,
                "unassigned_task_ids": compiled_assignment.unassigned_task_ids,
                "compiled_cluster_assignments": [
                    {
                        "cluster_id": item.cluster_id,
                        "task_ids_in_execution_order": item.task_ids_in_execution_order,
                        "impact_type": item.impact_type,
                        "placement_relation": item.placement_relation,
                        "batch_assignment_mode": item.batch_assignment_mode,
                        "target_batch_id": item.target_batch_id,
                        "target_batch_name": item.target_batch_name,
                        "intrabatch_placement_mode": item.intrabatch_placement_mode,
                        "anchor_task_id": item.anchor_task_id,
                        "rationale": item.rationale,
                    }
                    for item in compiled_assignment.compiled_cluster_assignments
                ],
                "notes": compiled_assignment.notes,
            },
        )

        return LivePlanMutationResult(
            mutation_kind="assignment",
            patched_execution_plan=patched_execution_plan,
            requires_replan=False,
            notes=list(compiled_assignment.notes),
            metadata={
                "assigned_task_ids": list(compiled_assignment.assigned_task_ids),
                "unassigned_task_ids": list(compiled_assignment.unassigned_task_ids),
                "compiled_cluster_assignments": [
                    {
                        "cluster_id": item.cluster_id,
                        "task_ids_in_execution_order": item.task_ids_in_execution_order,
                        "impact_type": item.impact_type,
                        "placement_relation": item.placement_relation,
                        "batch_assignment_mode": item.batch_assignment_mode,
                        "target_batch_id": item.target_batch_id,
                        "target_batch_name": item.target_batch_name,
                        "intrabatch_placement_mode": item.intrabatch_placement_mode,
                        "anchor_task_id": item.anchor_task_id,
                        "rationale": item.rationale,
                    }
                    for item in compiled_assignment.compiled_cluster_assignments
                ],
            },
        )

    if resolved_intent.intent_type == "resequence":
        if resolved_intent.mutation_scope != "resequence":
            raise LivePlanMutationServiceError(
                "Resolved resequence intent must carry mutation_scope='resequence'."
            )

        if not resolved_intent.requires_plan_mutation:
            raise LivePlanMutationServiceError(
                "Resolved resequence intent must require plan mutation."
            )

        if _should_run_immediate_resequence_patch(
            intent=resolved_intent,
            created_recovery_task_ids=created_recovery_task_ids,
            evaluation_decision=evaluation_decision,
        ):
            patched_execution_plan = insert_patch_batch_after_batch(
                plan=plan,
                anchor_batch_id=batch.batch_id,
                task_ids=created_recovery_task_ids,
                goal="Execute recovery work required before continuing the pending plan.",
                checkpoint_reason=(
                    "Validate the inserted recovery patch batch before continuing the remaining plan."
                ),
            )

            persist_patched_execution_plan(
                db=db,
                project_id=project.id,
                plan=patched_execution_plan,
                created_by="live_plan_mutation_service",
            )

            return LivePlanMutationResult(
                mutation_kind="resequence_patch",
                patched_execution_plan=patched_execution_plan,
                requires_replan=False,
                notes=[
                    "A local patch batch was inserted into the current plan to execute recovery-created work before continuing."
                ],
                metadata={
                    "patched_task_ids": list(created_recovery_task_ids),
                    "anchor_batch_id": batch.batch_id,
                },
            )

        return LivePlanMutationResult(
            mutation_kind="resequence_deferred",
            patched_execution_plan=None,
            requires_replan=False,
            notes=[
                "The remaining plan requires resequencing, but no immediate local patch batch was materialized."
            ],
            metadata={
                "patched_task_ids": [],
                "anchor_batch_id": batch.batch_id,
            },
        )

    if resolved_intent.intent_type in {"continue", "manual_review", "close", "replan"}:
        return LivePlanMutationResult(
            mutation_kind="none",
            patched_execution_plan=None,
            requires_replan=False,
            notes=[],
            metadata={},
        )

    raise LivePlanMutationServiceError(
        f"Unsupported resolved intent_type '{resolved_intent.intent_type}' for live plan mutation."
    )
