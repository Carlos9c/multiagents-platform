from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.schemas.execution_plan import (
    CheckpointDefinition,
    ExecutionBatch,
    ExecutionPlan,
)
from app.schemas.recovery_assignment import (
    AssignmentClusterProposal,
    AssignmentPlacementRelation,
    AssignmentTaskAssessment,
    RecoveryAssignmentInput,
    RecoveryAssignmentLLMOutput,
)


CompiledBatchAssignmentMode = Literal[
    "new_patch_batch",
    "attach_to_existing_batch",
]

CompiledIntraBatchPlacementMode = Literal[
    "prepend",
    "insert_before_task",
    "insert_after_task",
    "append",
    "not_applicable",
]


class RecoveryAssignmentCompilerError(Exception):
    """Raised when a recovery assignment proposal cannot be compiled safely."""


@dataclass(frozen=True)
class CompiledClusterAssignment:
    cluster_id: str
    task_ids_in_execution_order: list[int]
    impact_type: str
    placement_relation: AssignmentPlacementRelation

    batch_assignment_mode: CompiledBatchAssignmentMode
    target_batch_id: str | None
    target_batch_name: str | None

    intrabatch_placement_mode: CompiledIntraBatchPlacementMode
    anchor_task_id: int | None

    rationale: str


@dataclass(frozen=True)
class CompiledRecoveryAssignmentPlan:
    strategy: str
    requires_replan: bool
    compiled_cluster_assignments: list[CompiledClusterAssignment]
    patched_execution_plan: ExecutionPlan | None
    assigned_task_ids: list[int]
    unassigned_task_ids: list[int]
    notes: list[str]


def _dedupe_preserve_order(values: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _get_batch_or_raise(plan: ExecutionPlan, batch_id: str) -> ExecutionBatch:
    batch = next((item for item in plan.execution_batches if item.batch_id == batch_id), None)
    if batch is None:
        raise RecoveryAssignmentCompilerError(
            f"Batch '{batch_id}' not found in execution plan version {plan.plan_version}."
        )
    return batch


def _get_batch_position_or_raise(plan: ExecutionPlan, batch_id: str) -> int:
    for index, batch in enumerate(plan.execution_batches):
        if batch.batch_id == batch_id:
            return index
    raise RecoveryAssignmentCompilerError(
        f"Batch '{batch_id}' not found in execution plan version {plan.plan_version}."
    )


def _remaining_batches_after_current(
    plan: ExecutionPlan,
    *,
    current_batch_id: str,
) -> list[ExecutionBatch]:
    current_position = _get_batch_position_or_raise(plan, current_batch_id)
    return list(plan.execution_batches[current_position + 1 :])


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


def _insert_patch_batch_after_batch(
    *,
    plan: ExecutionPlan,
    anchor_batch_id: str,
    task_ids: list[int],
    goal: str,
    checkpoint_reason: str,
    risk_level: str = "medium",
) -> tuple[ExecutionPlan, ExecutionBatch]:
    if not task_ids:
        raise RecoveryAssignmentCompilerError(
            "Patch batch insertion requires at least one task id."
        )

    anchor_batch = _get_batch_or_raise(plan, anchor_batch_id)
    anchor_batch_index = anchor_batch.batch_index
    patch_index = _next_patch_index_for_anchor(
        plan=plan,
        anchor_batch_index=anchor_batch_index,
    )

    was_anchor_final_batch = plan.execution_batches[-1].batch_id == anchor_batch_id

    patch_checkpoint_id = _build_patch_checkpoint_id(
        plan_version=plan.plan_version,
        anchor_batch_index=anchor_batch_index,
        patch_index=patch_index,
    )

    patch_batch = ExecutionBatch(
        batch_internal_id=_build_patch_batch_internal_id(
            plan_version=plan.plan_version,
            anchor_batch_index=anchor_batch_index,
            patch_index=patch_index,
        ),
        batch_id=_build_patch_batch_id(
            plan_version=plan.plan_version,
            anchor_batch_index=anchor_batch_index,
            patch_index=patch_index,
        ),
        batch_index=anchor_batch_index,
        plan_version=plan.plan_version,
        name=_build_patch_batch_name(
            plan_version=plan.plan_version,
            anchor_batch_index=anchor_batch_index,
            patch_index=patch_index,
        ),
        goal=goal,
        task_ids=list(task_ids),
        entry_conditions=["Recovery assignment patch batch inserted into the live plan."],
        expected_outputs=["Recovery-assigned cluster executed and validated."],
        risk_level=risk_level,
        checkpoint_after=True,
        checkpoint_id=patch_checkpoint_id,
        checkpoint_reason=checkpoint_reason,
        is_patch_batch=True,
        anchor_batch_index=anchor_batch_index,
        patch_index=patch_index,
    )

    patch_evaluation_focus = ["functional_coverage", "dependency_validation"]
    if was_anchor_final_batch and "stage_closure" not in patch_evaluation_focus:
        patch_evaluation_focus.append("stage_closure")

    patch_checkpoint = CheckpointDefinition(
        checkpoint_id=patch_checkpoint_id,
        name=f"Patch checkpoint {anchor_batch_index}.{patch_index}",
        reason=checkpoint_reason,
        after_batch_id=patch_batch.batch_id,
        evaluation_goal="Evaluate whether the recovery-assigned cluster was integrated safely.",
        evaluation_focus=patch_evaluation_focus,
        can_introduce_new_tasks=True,
        can_resequence_remaining_work=True,
    )

    anchor_position = _get_batch_position_or_raise(plan, anchor_batch_id)
    insert_position = anchor_position + 1
    while (
        insert_position < len(plan.execution_batches)
        and plan.execution_batches[insert_position].is_patch_batch
        and plan.execution_batches[insert_position].anchor_batch_index == anchor_batch_index
    ):
        insert_position += 1

    patched_batches = list(plan.execution_batches)
    patched_batches.insert(insert_position, patch_batch)

    patched_checkpoints: list[CheckpointDefinition] = []
    for checkpoint in plan.checkpoints:
        if (
            was_anchor_final_batch
            and checkpoint.checkpoint_id == anchor_batch.checkpoint_id
            and "stage_closure" in checkpoint.evaluation_focus
        ):
            patched_checkpoints.append(
                CheckpointDefinition(
                    checkpoint_id=checkpoint.checkpoint_id,
                    name=checkpoint.name,
                    reason=checkpoint.reason,
                    after_batch_id=checkpoint.after_batch_id,
                    evaluation_goal=checkpoint.evaluation_goal,
                    evaluation_focus=[
                        item for item in checkpoint.evaluation_focus if item != "stage_closure"
                    ],
                    can_introduce_new_tasks=checkpoint.can_introduce_new_tasks,
                    can_resequence_remaining_work=checkpoint.can_resequence_remaining_work,
                )
            )
        else:
            patched_checkpoints.append(checkpoint)

    patched_checkpoints.append(patch_checkpoint)

    patched_plan = ExecutionPlan(
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

    return patched_plan, patch_batch


def _append_cluster_after_current_tail(
    *,
    plan: ExecutionPlan,
    task_ids: list[int],
    goal: str,
    checkpoint_reason: str,
) -> tuple[ExecutionPlan, ExecutionBatch]:
    final_batch = plan.execution_batches[-1]
    return _insert_patch_batch_after_batch(
        plan=plan,
        anchor_batch_id=final_batch.batch_id,
        task_ids=task_ids,
        goal=goal,
        checkpoint_reason=checkpoint_reason,
    )


def _replace_batch_in_plan(
    plan: ExecutionPlan,
    *,
    replacement_batch: ExecutionBatch,
) -> ExecutionPlan:
    replacement_batches: list[ExecutionBatch] = []
    replaced = False

    for batch in plan.execution_batches:
        if batch.batch_id == replacement_batch.batch_id:
            replacement_batches.append(replacement_batch)
            replaced = True
        else:
            replacement_batches.append(batch)

    if not replaced:
        raise RecoveryAssignmentCompilerError(
            f"Cannot replace batch '{replacement_batch.batch_id}' because it does not exist."
        )

    return ExecutionPlan(
        plan_version=plan.plan_version,
        supersedes_plan_version=plan.supersedes_plan_version,
        planning_scope=plan.planning_scope,
        global_goal=plan.global_goal,
        execution_batches=replacement_batches,
        checkpoints=list(plan.checkpoints),
        ready_task_ids=list(plan.ready_task_ids),
        blocked_task_ids=list(plan.blocked_task_ids),
        inferred_dependencies=list(plan.inferred_dependencies),
        sequencing_rationale=plan.sequencing_rationale,
        uncertainties=list(plan.uncertainties),
    )


def _cluster_goal(cluster: AssignmentClusterProposal) -> str:
    return f"Execute recovery assignment cluster '{cluster.cluster_id}'."


def _cluster_checkpoint_reason(cluster: AssignmentClusterProposal) -> str:
    return (
        f"Validate recovery assignment cluster '{cluster.cluster_id}' "
        f"({cluster.impact_type}) before continuing the live plan."
    )


def _cross_validate_input_and_output(
    *,
    assignment_input: RecoveryAssignmentInput,
    assignment_output: RecoveryAssignmentLLMOutput,
) -> None:
    input_task_ids = {task.task_id for task in assignment_input.new_tasks}
    output_task_ids = {task.task_id for task in assignment_output.task_assessments}

    if input_task_ids != output_task_ids:
        missing_from_output = sorted(input_task_ids.difference(output_task_ids))
        unexpected_in_output = sorted(output_task_ids.difference(input_task_ids))
        raise RecoveryAssignmentCompilerError(
            "LLM output does not match input new task coverage. "
            f"missing_from_output={missing_from_output}, "
            f"unexpected_in_output={unexpected_in_output}"
        )

    if assignment_input.resolved_intent_type == "assign":
        if assignment_input.resolved_mutation_scope != "assignment":
            raise RecoveryAssignmentCompilerError(
                "resolved_intent_type='assign' requires resolved_mutation_scope='assignment'."
            )
        expected_strategy = "continue_with_assignment"
    elif assignment_input.resolved_intent_type == "resequence":
        if assignment_input.resolved_mutation_scope != "resequence":
            raise RecoveryAssignmentCompilerError(
                "resolved_intent_type='resequence' requires resolved_mutation_scope='resequence'."
            )
        expected_strategy = "resequence_with_assignment"
    else:
        raise RecoveryAssignmentCompilerError(
            "RecoveryAssignmentInput only supports resolved_intent_type in "
            "{'assign', 'resequence'}."
        )

    if assignment_output.strategy == "requires_replan":
        structural_clusters = [
            cluster
            for cluster in assignment_output.clusters
            if cluster.impact_type == "structural_conflict"
        ]
        if not structural_clusters:
            raise RecoveryAssignmentCompilerError(
                "strategy='requires_replan' requires at least one structural_conflict cluster."
            )
    else:
        if assignment_output.strategy != expected_strategy:
            raise RecoveryAssignmentCompilerError(
                "LLM output strategy does not match the resolved assignment intent. "
                f"expected='{expected_strategy}', "
                f"got='{assignment_output.strategy}'."
            )

    if assignment_input.evaluation_signals.remaining_plan_still_valid is False:
        if assignment_output.strategy != "requires_replan":
            raise RecoveryAssignmentCompilerError(
                "remaining_plan_still_valid=False requires escalation to replan."
            )


def _assessment_maps(
    assignment_output: RecoveryAssignmentLLMOutput,
) -> tuple[dict[int, AssignmentTaskAssessment], dict[str, AssignmentClusterProposal]]:
    assessment_by_task_id = {
        assessment.task_id: assessment for assessment in assignment_output.task_assessments
    }
    cluster_by_id = {cluster.cluster_id: cluster for cluster in assignment_output.clusters}
    return assessment_by_task_id, cluster_by_id


def _build_existing_task_to_batch_map(plan: ExecutionPlan) -> dict[int, ExecutionBatch]:
    mapping: dict[int, ExecutionBatch] = {}
    for batch in plan.execution_batches:
        for task_id in batch.task_ids:
            mapping[task_id] = batch
    return mapping


def _existing_dependency_sets_for_cluster(
    *,
    cluster: AssignmentClusterProposal,
    assessment_by_task_id: dict[int, AssignmentTaskAssessment],
    assignment_input: RecoveryAssignmentInput,
) -> tuple[set[int], set[int]]:
    must_come_after_existing: set[int] = set()
    must_come_before_existing: set[int] = set()

    for task_id in cluster.task_ids_in_execution_order:
        assessment = assessment_by_task_id[task_id]
        must_come_after_existing.update(assessment.depends_on_existing_task_ids)

    for relationship in assignment_input.known_relationships.new_task_to_existing_task_dependencies:
        if relationship.new_task_id not in cluster.task_ids_in_execution_order:
            continue

        if relationship.relation == "depends_on_existing":
            must_come_after_existing.add(relationship.existing_task_id)
        elif relationship.relation in {
            "existing_depends_on_new",
            "possible_consumer_relation",
        }:
            must_come_before_existing.add(relationship.existing_task_id)

    return must_come_after_existing, must_come_before_existing


def _find_first_consumer_batch_or_raise(
    *,
    cluster: AssignmentClusterProposal,
    assessment_by_task_id: dict[int, AssignmentTaskAssessment],
    assignment_input: RecoveryAssignmentInput,
    plan: ExecutionPlan,
) -> ExecutionBatch:
    remaining_batches = _remaining_batches_after_current(
        plan,
        current_batch_id=assignment_input.live_plan_summary.current_batch_id,
    )
    if not remaining_batches:
        raise RecoveryAssignmentCompilerError(
            f"Cluster '{cluster.cluster_id}' requires a future consumer batch, but there are no remaining batches."
        )

    _, must_come_before_existing = _existing_dependency_sets_for_cluster(
        cluster=cluster,
        assessment_by_task_id=assessment_by_task_id,
        assignment_input=assignment_input,
    )

    existing_task_to_batch = _build_existing_task_to_batch_map(plan)

    candidate_batches: list[ExecutionBatch] = []
    candidate_task_ids = must_come_before_existing

    for task_id in candidate_task_ids:
        batch = existing_task_to_batch.get(task_id)
        if batch is None:
            continue
        if batch.batch_id == assignment_input.live_plan_summary.current_batch_id:
            continue
        if batch.batch_id in {item.batch_id for item in remaining_batches}:
            candidate_batches.append(batch)

    if candidate_batches:
        remaining_order = {batch.batch_id: index for index, batch in enumerate(remaining_batches)}
        return sorted(candidate_batches, key=lambda item: remaining_order[item.batch_id])[0]

    return remaining_batches[0]


def _compute_intrabatch_insertion_or_raise(
    *,
    cluster: AssignmentClusterProposal,
    target_batch: ExecutionBatch,
    assessment_by_task_id: dict[int, AssignmentTaskAssessment],
    assignment_input: RecoveryAssignmentInput,
) -> tuple[CompiledIntraBatchPlacementMode, int | None, list[int]]:
    (
        must_come_after_existing,
        must_come_before_existing,
    ) = _existing_dependency_sets_for_cluster(
        cluster=cluster,
        assessment_by_task_id=assessment_by_task_id,
        assignment_input=assignment_input,
    )

    batch_task_ids = list(target_batch.task_ids)
    batch_positions = {task_id: index for index, task_id in enumerate(batch_task_ids)}

    after_positions = sorted(
        batch_positions[task_id]
        for task_id in must_come_after_existing
        if task_id in batch_positions
    )
    before_positions = sorted(
        batch_positions[task_id]
        for task_id in must_come_before_existing
        if task_id in batch_positions
    )

    insert_index_min = 0
    insert_index_max = len(batch_task_ids)

    if after_positions:
        insert_index_min = max(after_positions) + 1
    if before_positions:
        insert_index_max = min(before_positions)

    if insert_index_min > insert_index_max:
        raise RecoveryAssignmentCompilerError(
            f"Cluster '{cluster.cluster_id}' cannot be inserted into batch '{target_batch.batch_id}' "
            "without breaking known dependency ordering."
        )

    insert_index = insert_index_min

    if before_positions and insert_index == 0:
        placement_mode: CompiledIntraBatchPlacementMode = "prepend"
        anchor_task_id = batch_task_ids[0]
    elif insert_index == len(batch_task_ids):
        placement_mode = "append"
        anchor_task_id = batch_task_ids[-1]
    elif insert_index > 0:
        placement_mode = "insert_after_task"
        anchor_task_id = batch_task_ids[insert_index - 1]
    else:
        placement_mode = "insert_before_task"
        anchor_task_id = batch_task_ids[0]

    reordered_task_ids = (
        batch_task_ids[:insert_index]
        + list(cluster.task_ids_in_execution_order)
        + batch_task_ids[insert_index:]
    )

    if len(set(reordered_task_ids)) != len(reordered_task_ids):
        raise RecoveryAssignmentCompilerError(
            f"Cluster '{cluster.cluster_id}' would duplicate task ids inside batch '{target_batch.batch_id}'."
        )

    return placement_mode, anchor_task_id, reordered_task_ids


def _attach_cluster_to_existing_batch(
    *,
    plan: ExecutionPlan,
    cluster: AssignmentClusterProposal,
    target_batch: ExecutionBatch,
    assessment_by_task_id: dict[int, AssignmentTaskAssessment],
    assignment_input: RecoveryAssignmentInput,
) -> tuple[ExecutionPlan, CompiledClusterAssignment]:
    (
        placement_mode,
        anchor_task_id,
        reordered_task_ids,
    ) = _compute_intrabatch_insertion_or_raise(
        cluster=cluster,
        target_batch=target_batch,
        assessment_by_task_id=assessment_by_task_id,
        assignment_input=assignment_input,
    )

    replacement_batch = ExecutionBatch(
        batch_internal_id=target_batch.batch_internal_id,
        batch_id=target_batch.batch_id,
        batch_index=target_batch.batch_index,
        plan_version=target_batch.plan_version,
        name=target_batch.name,
        goal=target_batch.goal,
        task_ids=reordered_task_ids,
        entry_conditions=list(target_batch.entry_conditions),
        expected_outputs=list(target_batch.expected_outputs),
        risk_level=target_batch.risk_level,
        checkpoint_after=target_batch.checkpoint_after,
        checkpoint_id=target_batch.checkpoint_id,
        checkpoint_reason=target_batch.checkpoint_reason,
        is_patch_batch=target_batch.is_patch_batch,
        anchor_batch_index=target_batch.anchor_batch_index,
        patch_index=target_batch.patch_index,
    )

    patched_plan = _replace_batch_in_plan(
        plan,
        replacement_batch=replacement_batch,
    )

    compiled_assignment = CompiledClusterAssignment(
        cluster_id=cluster.cluster_id,
        task_ids_in_execution_order=list(cluster.task_ids_in_execution_order),
        impact_type=cluster.impact_type,
        placement_relation=cluster.placement_relation,
        batch_assignment_mode="attach_to_existing_batch",
        target_batch_id=target_batch.batch_id,
        target_batch_name=target_batch.name,
        intrabatch_placement_mode=placement_mode,
        anchor_task_id=anchor_task_id,
        rationale=cluster.rationale,
    )

    return patched_plan, compiled_assignment


def _materialize_cluster_as_patch_after_batch(
    *,
    plan: ExecutionPlan,
    cluster: AssignmentClusterProposal,
    anchor_batch_id: str,
) -> tuple[ExecutionPlan, CompiledClusterAssignment]:
    patched_plan, created_patch_batch = _insert_patch_batch_after_batch(
        plan=plan,
        anchor_batch_id=anchor_batch_id,
        task_ids=list(cluster.task_ids_in_execution_order),
        goal=_cluster_goal(cluster),
        checkpoint_reason=_cluster_checkpoint_reason(cluster),
    )

    compiled_assignment = CompiledClusterAssignment(
        cluster_id=cluster.cluster_id,
        task_ids_in_execution_order=list(cluster.task_ids_in_execution_order),
        impact_type=cluster.impact_type,
        placement_relation=cluster.placement_relation,
        batch_assignment_mode="new_patch_batch",
        target_batch_id=created_patch_batch.batch_id,
        target_batch_name=created_patch_batch.name,
        intrabatch_placement_mode="not_applicable",
        anchor_task_id=None,
        rationale=cluster.rationale,
    )

    return patched_plan, compiled_assignment


def _compile_cluster(
    *,
    plan: ExecutionPlan,
    cluster: AssignmentClusterProposal,
    assessment_by_task_id: dict[int, AssignmentTaskAssessment],
    assignment_input: RecoveryAssignmentInput,
) -> tuple[ExecutionPlan, CompiledClusterAssignment]:
    current_batch_id = assignment_input.live_plan_summary.current_batch_id

    if cluster.impact_type == "structural_conflict":
        raise RecoveryAssignmentCompilerError(
            f"Cluster '{cluster.cluster_id}' is structural_conflict and cannot be compiled into the current plan."
        )

    if cluster.placement_relation == "before_next_useful_progress":
        return _materialize_cluster_as_patch_after_batch(
            plan=plan,
            cluster=cluster,
            anchor_batch_id=current_batch_id,
        )

    if cluster.placement_relation == "after_current_tail":
        patched_plan, created_patch_batch = _append_cluster_after_current_tail(
            plan=plan,
            task_ids=list(cluster.task_ids_in_execution_order),
            goal=_cluster_goal(cluster),
            checkpoint_reason=_cluster_checkpoint_reason(cluster),
        )
        compiled_assignment = CompiledClusterAssignment(
            cluster_id=cluster.cluster_id,
            task_ids_in_execution_order=list(cluster.task_ids_in_execution_order),
            impact_type=cluster.impact_type,
            placement_relation=cluster.placement_relation,
            batch_assignment_mode="new_patch_batch",
            target_batch_id=created_patch_batch.batch_id,
            target_batch_name=created_patch_batch.name,
            intrabatch_placement_mode="not_applicable",
            anchor_task_id=None,
            rationale=cluster.rationale,
        )
        return patched_plan, compiled_assignment

    if cluster.placement_relation == "before_first_consumer_batch":
        target_batch = _find_first_consumer_batch_or_raise(
            cluster=cluster,
            assessment_by_task_id=assessment_by_task_id,
            assignment_input=assignment_input,
            plan=plan,
        )
        return _attach_cluster_to_existing_batch(
            plan=plan,
            cluster=cluster,
            target_batch=target_batch,
            assessment_by_task_id=assessment_by_task_id,
            assignment_input=assignment_input,
        )

    if cluster.placement_relation == "requires_replan":
        raise RecoveryAssignmentCompilerError(
            f"Cluster '{cluster.cluster_id}' requires replan and cannot be compiled into the current plan."
        )

    raise RecoveryAssignmentCompilerError(
        f"Unsupported placement_relation '{cluster.placement_relation}' for cluster '{cluster.cluster_id}'."
    )


def _ensure_no_new_task_remains_unassigned(
    *,
    assignment_input: RecoveryAssignmentInput,
    compiled_cluster_assignments: list[CompiledClusterAssignment],
) -> None:
    expected_task_ids = {task.task_id for task in assignment_input.new_tasks}
    assigned_task_ids: set[int] = set()

    for compiled in compiled_cluster_assignments:
        assigned_task_ids.update(compiled.task_ids_in_execution_order)

    if expected_task_ids != assigned_task_ids:
        missing = sorted(expected_task_ids.difference(assigned_task_ids))
        unexpected = sorted(assigned_task_ids.difference(expected_task_ids))
        raise RecoveryAssignmentCompilerError(
            "Compiled assignment does not cover exactly the input new tasks. "
            f"missing={missing}, unexpected={unexpected}"
        )


def compile_recovery_assignment_plan(
    *,
    plan: ExecutionPlan,
    assignment_input: RecoveryAssignmentInput,
    assignment_output: RecoveryAssignmentLLMOutput,
) -> CompiledRecoveryAssignmentPlan:
    _cross_validate_input_and_output(
        assignment_input=assignment_input,
        assignment_output=assignment_output,
    )

    if assignment_output.strategy == "requires_replan":
        task_ids = sorted(task.task_id for task in assignment_input.new_tasks)
        return CompiledRecoveryAssignmentPlan(
            strategy=assignment_output.strategy,
            requires_replan=True,
            compiled_cluster_assignments=[],
            patched_execution_plan=None,
            assigned_task_ids=[],
            unassigned_task_ids=task_ids,
            notes=list(assignment_output.notes),
        )

    if not assignment_output.clusters:
        raise RecoveryAssignmentCompilerError(
            "Recovery assignment output cannot be compiled because it contains no clusters."
        )

    assessment_by_task_id, _ = _assessment_maps(assignment_output)

    current_plan = plan
    compiled_assignments: list[CompiledClusterAssignment] = []

    cluster_priority = {
        "immediate_blocking": 0,
        "future_blocking": 1,
        "corrective_local": 2,
        "additive_deferred": 3,
        "structural_conflict": 4,
    }

    ordered_clusters = sorted(
        assignment_output.clusters,
        key=lambda item: (
            cluster_priority.get(item.impact_type, 99),
            item.cluster_id,
        ),
    )

    for cluster in ordered_clusters:
        current_plan, compiled_cluster_assignment = _compile_cluster(
            plan=current_plan,
            cluster=cluster,
            assessment_by_task_id=assessment_by_task_id,
            assignment_input=assignment_input,
        )
        compiled_assignments.append(compiled_cluster_assignment)

    _ensure_no_new_task_remains_unassigned(
        assignment_input=assignment_input,
        compiled_cluster_assignments=compiled_assignments,
    )

    assigned_task_ids = _dedupe_preserve_order(
        [
            task_id
            for compiled in compiled_assignments
            for task_id in compiled.task_ids_in_execution_order
        ]
    )

    return CompiledRecoveryAssignmentPlan(
        strategy=assignment_output.strategy,
        requires_replan=False,
        compiled_cluster_assignments=compiled_assignments,
        patched_execution_plan=current_plan,
        assigned_task_ids=assigned_task_ids,
        unassigned_task_ids=[],
        notes=list(assignment_output.notes),
    )
