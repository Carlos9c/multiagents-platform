import pytest

from app.schemas.recovery_assignment import (
    AssignmentClusterProposal,
    AssignmentEvaluationSignals,
    AssignmentRecoverySignal,
    AssignmentRecoverySignals,
    AssignmentTaskAssessment,
    ExecutedBatchAssignmentSummary,
    KnownAssignmentRelationships,
    LivePlanSummaryForAssignment,
    NewTaskExistingTaskRelationship,
    NextUsefulProgressSummary,
    RecoveryAssignmentInput,
    RecoveryAssignmentLLMOutput,
    RecoveryTaskForAssignment,
    RemainingBatchSummary,
)
from app.services.recovery_assignment_compiler_service import (
    RecoveryAssignmentCompilerError,
    compile_recovery_assignment_plan,
)


def _plan_batch(
    *,
    batch_id: str,
    task_ids: list[int],
    plan_version: int = 2,
    batch_index: int | None = None,
) -> dict:
    if batch_index is None:
        suffix = batch_id.rsplit("_", 1)[-1]
        batch_index = int(suffix)
    return {
        "batch_id": batch_id,
        "batch_internal_id": f"{plan_version}_{batch_index}",
        "batch_index": batch_index,
        "plan_version": plan_version,
        "task_ids": list(task_ids),
    }


def _recovery_task(task) -> RecoveryTaskForAssignment:
    return RecoveryTaskForAssignment(
        task_id=task.id,
        title=task.title,
        description=task.description,
    )


def _build_assignment_input(
    *,
    project_id: int,
    new_tasks: list[RecoveryTaskForAssignment],
    live_plan_summary: LivePlanSummaryForAssignment,
    resolved_intent_type: str = "assign",
    resolved_mutation_scope: str = "assignment",
    remaining_plan_still_valid: bool = True,
    new_recovery_tasks_blocking: bool | None = None,
    known_relationships: KnownAssignmentRelationships | None = None,
    next_useful_progress: NextUsefulProgressSummary | None = None,
) -> RecoveryAssignmentInput:
    return RecoveryAssignmentInput(
        project_id=project_id,
        project_goal="Ship the current project stage safely.",
        current_stage_summary="The project is in the middle of the current stage.",
        resolved_intent_type=resolved_intent_type,
        resolved_mutation_scope=resolved_mutation_scope,
        executed_batch_summary=ExecutedBatchAssignmentSummary(
            batch_id=live_plan_summary.current_batch_id,
            batch_name=live_plan_summary.current_batch_name,
            goal="Complete the current batch.",
            executed_task_ids=[101],
            completed_task_ids=[101],
            partial_task_ids=[],
            failed_task_ids=[],
            summary="The current batch finished and produced valid outputs.",
            key_findings=["The batch completed successfully."],
        ),
        evaluation_signals=AssignmentEvaluationSignals(
            decision="stage_incomplete",
            decision_summary="The project should continue with controlled assignment.",
            recommended_next_action=resolved_intent_type,
            recommended_next_action_reason="The current plan can continue with the new work assigned safely.",
            plan_change_scope="none"
            if resolved_intent_type == "assign"
            else "local_resequencing",
            remaining_plan_still_valid=remaining_plan_still_valid,
            new_recovery_tasks_blocking=new_recovery_tasks_blocking,
            single_task_tail_risk=False,
            decision_signals=["remaining_plan_still_valid"],
            key_risks=[],
            notes=["Assignment is required before the next batch starts."],
        ),
        recovery_signals=AssignmentRecoverySignals(
            entries=[
                AssignmentRecoverySignal(
                    source_task_id=101,
                    source_run_id=201,
                    recovery_action="create_followup_tasks",
                    recovery_reason="Recovery created local follow-up work.",
                    covered_gap_summary="The new work closes a gap detected after execution.",
                    still_blocks_progress=bool(new_recovery_tasks_blocking),
                    execution_guidance="Assign the new work into the live plan.",
                    evaluation_guidance="Keep the remaining plan stable when possible.",
                )
            ]
        ),
        new_tasks=new_tasks,
        live_plan_summary=live_plan_summary,
        next_useful_progress=next_useful_progress,
        pending_valid_tasks=[],
        known_relationships=known_relationships or KnownAssignmentRelationships(),
    )


def _build_live_plan_summary_from_plan(plan) -> LivePlanSummaryForAssignment:
    current_batch = plan.execution_batches[0]
    remaining_batches = plan.execution_batches[1:]

    return LivePlanSummaryForAssignment(
        plan_version=plan.plan_version,
        current_batch_id=current_batch.batch_id,
        current_batch_name=current_batch.name,
        remaining_batches=[
            RemainingBatchSummary(
                batch_id=batch.batch_id,
                batch_name=batch.name,
                batch_index=batch.batch_index,
                goal=batch.goal,
                task_ids=list(batch.task_ids),
                task_titles=[f"Task {task_id}" for task_id in batch.task_ids],
                checkpoint_reason=batch.checkpoint_reason,
                is_patch_batch=batch.is_patch_batch,
            )
            for batch in remaining_batches
        ],
    )


def test_compile_recovery_assignment_plan_inserts_immediate_blocking_cluster_as_patch_batch(
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(project_id=project.id, title="Current batch task")
    next_task = make_task(project_id=project.id, title="Next batch task")
    new_task_1 = make_task(project_id=project.id, title="Recovery task 1")
    new_task_2 = make_task(project_id=project.id, title="Recovery task 2")

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            _plan_batch(batch_id="plan_2_batch_1", task_ids=[current_task.id]),
            _plan_batch(batch_id="plan_2_batch_2", task_ids=[next_task.id]),
        ],
    )

    assignment_input = _build_assignment_input(
        project_id=project.id,
        new_tasks=[_recovery_task(new_task_1), _recovery_task(new_task_2)],
        live_plan_summary=_build_live_plan_summary_from_plan(plan),
        resolved_intent_type="assign",
        resolved_mutation_scope="assignment",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=True,
        next_useful_progress=NextUsefulProgressSummary(
            summary="Proceed to the next batch after the blocking recovery work.",
            task_ids=[next_task.id],
            batch_id="plan_2_batch_2",
            batch_name="Plan 2 · Batch 2",
        ),
    )

    assignment_output = RecoveryAssignmentLLMOutput(
        strategy="continue_with_assignment",
        task_assessments=[
            AssignmentTaskAssessment(
                task_id=new_task_1.id,
                impact_type="immediate_blocking",
                grouping_role="core",
                suggested_cluster_id="cluster_1",
                depends_on_new_task_ids=[],
                depends_on_existing_task_ids=[],
                rationale="This task must run before the next useful progress.",
            ),
            AssignmentTaskAssessment(
                task_id=new_task_2.id,
                impact_type="immediate_blocking",
                grouping_role="dependent",
                suggested_cluster_id="cluster_1",
                depends_on_new_task_ids=[new_task_1.id],
                depends_on_existing_task_ids=[],
                rationale="This task depends on the first recovery task and must also run immediately.",
            ),
        ],
        clusters=[
            AssignmentClusterProposal(
                cluster_id="cluster_1",
                task_ids_in_execution_order=[new_task_1.id, new_task_2.id],
                impact_type="immediate_blocking",
                grouped_execution_required=True,
                placement_relation="before_next_useful_progress",
                rationale="These tasks form one immediate blocking chain.",
            )
        ],
        notes=["Insert the blocking cluster before the next useful progress."],
    )

    compiled = compile_recovery_assignment_plan(
        plan=plan,
        assignment_input=assignment_input,
        assignment_output=assignment_output,
    )

    assert compiled.requires_replan is False
    assert compiled.unassigned_task_ids == []
    assert compiled.assigned_task_ids == [new_task_1.id, new_task_2.id]
    assert compiled.patched_execution_plan is not None
    assert len(compiled.compiled_cluster_assignments) == 1

    cluster_assignment = compiled.compiled_cluster_assignments[0]
    assert cluster_assignment.cluster_id == "cluster_1"
    assert cluster_assignment.batch_assignment_mode == "new_patch_batch"
    assert cluster_assignment.intrabatch_placement_mode == "not_applicable"

    patched_batches = compiled.patched_execution_plan.execution_batches
    assert len(patched_batches) == 3
    assert patched_batches[1].is_patch_batch is True
    assert patched_batches[1].task_ids == [new_task_1.id, new_task_2.id]
    assert patched_batches[1].anchor_batch_index == 1
    assert patched_batches[1].patch_index == 1


def test_compile_recovery_assignment_plan_attaches_future_blocking_cluster_inside_consumer_batch(
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(project_id=project.id, title="Current batch task")
    consumer_before = make_task(project_id=project.id, title="Consumer pre-task")
    consumer_target = make_task(project_id=project.id, title="Consumer task")
    consumer_after = make_task(project_id=project.id, title="Trailing task")

    new_task_1 = make_task(project_id=project.id, title="Recovery task A")
    new_task_2 = make_task(project_id=project.id, title="Recovery task B")

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            _plan_batch(batch_id="plan_2_batch_1", task_ids=[current_task.id]),
            _plan_batch(
                batch_id="plan_2_batch_2",
                task_ids=[consumer_before.id, consumer_target.id, consumer_after.id],
            ),
        ],
    )

    known_relationships = KnownAssignmentRelationships(
        new_task_to_existing_task_dependencies=[
            NewTaskExistingTaskRelationship(
                new_task_id=new_task_1.id,
                existing_task_id=consumer_before.id,
                relation="depends_on_existing",
                reason="The first recovery task depends on the consumer pre-task output.",
            ),
            NewTaskExistingTaskRelationship(
                new_task_id=new_task_2.id,
                existing_task_id=consumer_target.id,
                relation="existing_depends_on_new",
                reason="The consumer task must happen after the recovery cluster.",
            ),
        ]
    )

    assignment_input = _build_assignment_input(
        project_id=project.id,
        new_tasks=[_recovery_task(new_task_1), _recovery_task(new_task_2)],
        live_plan_summary=_build_live_plan_summary_from_plan(plan),
        resolved_intent_type="assign",
        resolved_mutation_scope="assignment",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        known_relationships=known_relationships,
        next_useful_progress=NextUsefulProgressSummary(
            summary="Continue the next batch and inject the future-blocking recovery work before its consumer task.",
            task_ids=[consumer_before.id, consumer_target.id],
            batch_id="plan_2_batch_2",
            batch_name="Plan 2 · Batch 2",
        ),
    )

    assignment_output = RecoveryAssignmentLLMOutput(
        strategy="continue_with_assignment",
        task_assessments=[
            AssignmentTaskAssessment(
                task_id=new_task_1.id,
                impact_type="future_blocking",
                grouping_role="core",
                suggested_cluster_id="cluster_1",
                depends_on_new_task_ids=[],
                depends_on_existing_task_ids=[consumer_before.id],
                rationale="The first recovery task depends on an existing pre-task in the consumer batch.",
            ),
            AssignmentTaskAssessment(
                task_id=new_task_2.id,
                impact_type="future_blocking",
                grouping_role="dependent",
                suggested_cluster_id="cluster_1",
                depends_on_new_task_ids=[new_task_1.id],
                depends_on_existing_task_ids=[],
                rationale="The second recovery task depends on the first recovery task.",
            ),
        ],
        clusters=[
            AssignmentClusterProposal(
                cluster_id="cluster_1",
                task_ids_in_execution_order=[new_task_1.id, new_task_2.id],
                impact_type="future_blocking",
                grouped_execution_required=True,
                placement_relation="before_first_consumer_batch",
                rationale="This recovery chain must be inserted before the first consumer work.",
            )
        ],
        notes=["Attach the future-blocking cluster inside the first consumer batch."],
    )

    compiled = compile_recovery_assignment_plan(
        plan=plan,
        assignment_input=assignment_input,
        assignment_output=assignment_output,
    )

    assert compiled.requires_replan is False
    assert compiled.unassigned_task_ids == []
    assert compiled.assigned_task_ids == [new_task_1.id, new_task_2.id]
    assert compiled.patched_execution_plan is not None
    assert len(compiled.compiled_cluster_assignments) == 1

    cluster_assignment = compiled.compiled_cluster_assignments[0]
    assert cluster_assignment.cluster_id == "cluster_1"
    assert cluster_assignment.batch_assignment_mode == "attach_to_existing_batch"
    assert cluster_assignment.target_batch_id == "plan_2_batch_2"
    assert cluster_assignment.intrabatch_placement_mode == "insert_after_task"
    assert cluster_assignment.anchor_task_id == consumer_before.id

    patched_batch = compiled.patched_execution_plan.execution_batches[1]
    assert patched_batch.batch_id == "plan_2_batch_2"
    assert patched_batch.task_ids == [
        consumer_before.id,
        new_task_1.id,
        new_task_2.id,
        consumer_target.id,
        consumer_after.id,
    ]


def test_compile_recovery_assignment_plan_appends_deferred_cluster_after_current_tail(
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(project_id=project.id, title="Current batch task")
    future_task = make_task(project_id=project.id, title="Future batch task")
    new_task = make_task(project_id=project.id, title="Deferred recovery task")

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            _plan_batch(batch_id="plan_2_batch_1", task_ids=[current_task.id]),
            _plan_batch(batch_id="plan_2_batch_2", task_ids=[future_task.id]),
        ],
    )

    assignment_input = _build_assignment_input(
        project_id=project.id,
        new_tasks=[_recovery_task(new_task)],
        live_plan_summary=_build_live_plan_summary_from_plan(plan),
        resolved_intent_type="assign",
        resolved_mutation_scope="assignment",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        next_useful_progress=NextUsefulProgressSummary(
            summary="The next useful progress is already represented by the next batch.",
            task_ids=[future_task.id],
            batch_id="plan_2_batch_2",
            batch_name="Plan 2 · Batch 2",
        ),
    )

    assignment_output = RecoveryAssignmentLLMOutput(
        strategy="continue_with_assignment",
        task_assessments=[
            AssignmentTaskAssessment(
                task_id=new_task.id,
                impact_type="additive_deferred",
                grouping_role="independent",
                suggested_cluster_id="cluster_1",
                depends_on_new_task_ids=[],
                depends_on_existing_task_ids=[],
                rationale="This new work can safely be deferred to the plan tail.",
            )
        ],
        clusters=[
            AssignmentClusterProposal(
                cluster_id="cluster_1",
                task_ids_in_execution_order=[new_task.id],
                impact_type="additive_deferred",
                grouped_execution_required=False,
                placement_relation="after_current_tail",
                rationale="The work is additive and can be appended after the current plan tail.",
            )
        ],
        notes=["Append the additive cluster to the tail."],
    )

    compiled = compile_recovery_assignment_plan(
        plan=plan,
        assignment_input=assignment_input,
        assignment_output=assignment_output,
    )

    assert compiled.requires_replan is False
    assert compiled.assigned_task_ids == [new_task.id]
    assert compiled.unassigned_task_ids == []
    assert compiled.patched_execution_plan is not None
    assert len(compiled.compiled_cluster_assignments) == 1

    cluster_assignment = compiled.compiled_cluster_assignments[0]
    assert cluster_assignment.batch_assignment_mode == "new_patch_batch"
    assert cluster_assignment.intrabatch_placement_mode == "not_applicable"

    patched_batches = compiled.patched_execution_plan.execution_batches
    assert len(patched_batches) == 3
    assert patched_batches[-1].is_patch_batch is True
    assert patched_batches[-1].task_ids == [new_task.id]
    assert patched_batches[-1].anchor_batch_index == 2
    assert patched_batches[-1].patch_index == 1


def test_compile_recovery_assignment_plan_returns_requires_replan_without_patching_plan(
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(project_id=project.id, title="Current batch task")
    future_task = make_task(project_id=project.id, title="Future batch task")
    conflicting_new_task = make_task(
        project_id=project.id, title="Structural conflict task"
    )

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            _plan_batch(batch_id="plan_2_batch_1", task_ids=[current_task.id]),
            _plan_batch(batch_id="plan_2_batch_2", task_ids=[future_task.id]),
        ],
    )

    assignment_input = _build_assignment_input(
        project_id=project.id,
        new_tasks=[_recovery_task(conflicting_new_task)],
        live_plan_summary=_build_live_plan_summary_from_plan(plan),
        resolved_intent_type="assign",
        resolved_mutation_scope="assignment",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
    )

    assignment_output = RecoveryAssignmentLLMOutput(
        strategy="requires_replan",
        task_assessments=[
            AssignmentTaskAssessment(
                task_id=conflicting_new_task.id,
                impact_type="structural_conflict",
                grouping_role="core",
                suggested_cluster_id="cluster_1",
                depends_on_new_task_ids=[],
                depends_on_existing_task_ids=[],
                rationale="This task reveals a structural contradiction in the remaining plan.",
            )
        ],
        clusters=[
            AssignmentClusterProposal(
                cluster_id="cluster_1",
                task_ids_in_execution_order=[conflicting_new_task.id],
                impact_type="structural_conflict",
                grouped_execution_required=True,
                placement_relation="requires_replan",
                rationale="The remaining plan is no longer structurally valid.",
            )
        ],
        notes=["Escalate to replan because the conflict is structural."],
    )

    compiled = compile_recovery_assignment_plan(
        plan=plan,
        assignment_input=assignment_input,
        assignment_output=assignment_output,
    )

    assert compiled.requires_replan is True
    assert compiled.patched_execution_plan is None
    assert compiled.assigned_task_ids == []
    assert compiled.unassigned_task_ids == [conflicting_new_task.id]
    assert compiled.compiled_cluster_assignments == []


def test_compile_recovery_assignment_plan_rejects_strategy_that_conflicts_with_resolved_assignment_intent(
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(project_id=project.id, title="Current batch task")
    future_task = make_task(project_id=project.id, title="Future batch task")
    new_task = make_task(project_id=project.id, title="Recovery task")

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            _plan_batch(batch_id="plan_2_batch_1", task_ids=[current_task.id]),
            _plan_batch(batch_id="plan_2_batch_2", task_ids=[future_task.id]),
        ],
    )

    assignment_input = _build_assignment_input(
        project_id=project.id,
        new_tasks=[_recovery_task(new_task)],
        live_plan_summary=_build_live_plan_summary_from_plan(plan),
        resolved_intent_type="assign",
        resolved_mutation_scope="assignment",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
    )

    assignment_output = RecoveryAssignmentLLMOutput(
        strategy="resequence_with_assignment",
        task_assessments=[
            AssignmentTaskAssessment(
                task_id=new_task.id,
                impact_type="additive_deferred",
                grouping_role="independent",
                suggested_cluster_id="cluster_1",
                depends_on_new_task_ids=[],
                depends_on_existing_task_ids=[],
                rationale="This task is additive and does not block immediate progress.",
            )
        ],
        clusters=[
            AssignmentClusterProposal(
                cluster_id="cluster_1",
                task_ids_in_execution_order=[new_task.id],
                impact_type="additive_deferred",
                grouped_execution_required=False,
                placement_relation="after_current_tail",
                rationale="The work can be deferred to the tail.",
            )
        ],
        notes=[
            "This output intentionally conflicts with the resolved assignment intent."
        ],
    )

    with pytest.raises(RecoveryAssignmentCompilerError) as exc_info:
        compile_recovery_assignment_plan(
            plan=plan,
            assignment_input=assignment_input,
            assignment_output=assignment_output,
        )

    assert "does not match the resolved assignment intent" in str(exc_info.value)


def test_compile_recovery_assignment_plan_rejects_intrabatch_insertion_when_dependency_window_is_impossible(
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(project_id=project.id, title="Current batch task")
    target_before = make_task(project_id=project.id, title="Target before")
    target_middle = make_task(project_id=project.id, title="Target middle")
    target_after = make_task(project_id=project.id, title="Target after")
    new_task = make_task(project_id=project.id, title="Impossible recovery task")

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            _plan_batch(batch_id="plan_2_batch_1", task_ids=[current_task.id]),
            _plan_batch(
                batch_id="plan_2_batch_2",
                task_ids=[target_before.id, target_middle.id, target_after.id],
            ),
        ],
    )

    known_relationships = KnownAssignmentRelationships(
        new_task_to_existing_task_dependencies=[
            NewTaskExistingTaskRelationship(
                new_task_id=new_task.id,
                existing_task_id=target_after.id,
                relation="depends_on_existing",
                reason="The new task depends on the last task in the target batch.",
            ),
            NewTaskExistingTaskRelationship(
                new_task_id=new_task.id,
                existing_task_id=target_before.id,
                relation="existing_depends_on_new",
                reason="The first task in the target batch should happen after the new task.",
            ),
        ]
    )

    assignment_input = _build_assignment_input(
        project_id=project.id,
        new_tasks=[_recovery_task(new_task)],
        live_plan_summary=_build_live_plan_summary_from_plan(plan),
        resolved_intent_type="assign",
        resolved_mutation_scope="assignment",
        remaining_plan_still_valid=True,
        new_recovery_tasks_blocking=False,
        known_relationships=known_relationships,
        next_useful_progress=NextUsefulProgressSummary(
            summary="The cluster should be injected before the first consumer batch if possible.",
            task_ids=[target_before.id, target_middle.id, target_after.id],
            batch_id="plan_2_batch_2",
            batch_name="Plan 2 · Batch 2",
        ),
    )

    assignment_output = RecoveryAssignmentLLMOutput(
        strategy="continue_with_assignment",
        task_assessments=[
            AssignmentTaskAssessment(
                task_id=new_task.id,
                impact_type="future_blocking",
                grouping_role="core",
                suggested_cluster_id="cluster_1",
                depends_on_new_task_ids=[],
                depends_on_existing_task_ids=[target_after.id],
                rationale="This task depends on the last task of the target batch.",
            )
        ],
        clusters=[
            AssignmentClusterProposal(
                cluster_id="cluster_1",
                task_ids_in_execution_order=[new_task.id],
                impact_type="future_blocking",
                grouped_execution_required=True,
                placement_relation="before_first_consumer_batch",
                rationale="This cluster is intentionally impossible to place safely inside the batch.",
            )
        ],
        notes=["The known relationships should make intrabatch placement impossible."],
    )

    with pytest.raises(RecoveryAssignmentCompilerError) as exc_info:
        compile_recovery_assignment_plan(
            plan=plan,
            assignment_input=assignment_input,
            assignment_output=assignment_output,
        )

    assert "cannot be inserted into batch" in str(exc_info.value)
