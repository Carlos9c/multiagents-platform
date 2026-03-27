import types

from app.models.artifact import Artifact
from app.models.task import TASK_STATUS_PENDING
from app.services.live_plan_mutation_service import mutate_live_plan


def test_mutate_live_plan_assignment_returns_patched_plan(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(
        project_id=project.id,
        title="Current batch task",
        status=TASK_STATUS_PENDING,
    )
    new_recovery_task = make_task(
        project_id=project.id,
        title="Recovery task",
        status=TASK_STATUS_PENDING,
    )
    future_task = make_task(
        project_id=project.id,
        title="Future task",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [current_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )
    batch = plan.execution_batches[0]

    compiled_plan = make_execution_plan(
        plan_version=2,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [current_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_1_patch_1",
                "batch_internal_id": "2_1_p1",
                "batch_index": 1,
                "plan_version": 2,
                "task_ids": [new_recovery_task.id],
                "checkpoint_id": "checkpoint_batch_1_patch_1",
                "checkpoint_name": "Patch checkpoint 1.1",
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )

    resolved_intent = types.SimpleNamespace(
        intent_type="assign",
        legacy_action="continue_current_plan",
        remaining_plan_still_valid=True,
    )

    evaluation_decision = types.SimpleNamespace(
        decision="stage_incomplete",
        recommended_next_action="continue_current_plan",
        remaining_plan_still_valid=True,
    )

    recovery_context = types.SimpleNamespace(
        recovery_decisions=[],
        recovery_created_tasks=[],
    )

    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.call_recovery_assignment_model",
        lambda **kwargs: types.SimpleNamespace(
            model_dump=lambda mode="json": {
                "strategy": "continue_with_assignment",
                "clusters": [{"cluster_id": "cluster_1"}],
            }
        ),
    )

    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.compile_recovery_assignment_plan",
        lambda **kwargs: types.SimpleNamespace(
            strategy="continue_with_assignment",
            requires_replan=False,
            patched_execution_plan=compiled_plan,
            assigned_task_ids=[new_recovery_task.id],
            unassigned_task_ids=[],
            compiled_cluster_assignments=[
                types.SimpleNamespace(
                    cluster_id="cluster_1",
                    task_ids_in_execution_order=[new_recovery_task.id],
                    impact_type="additive_deferred",
                    placement_relation="after_current_tail",
                    batch_assignment_mode="new_patch_batch",
                    target_batch_id="batch_1_patch_1",
                    target_batch_name="Plan 2 · Batch 1.1",
                    intrabatch_placement_mode="not_applicable",
                    anchor_task_id=None,
                    rationale="Append recovery work as a patch batch.",
                )
            ],
            notes=["All new work was assigned safely."],
        ),
    )

    payload_artifacts = []

    def _persist_payload(**kwargs):
        payload_artifacts.append(kwargs["artifact_type"])

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=evaluation_decision,
        recovery_context=recovery_context,
        created_recovery_task_ids=[new_recovery_task.id],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[],
        build_recovery_assignment_input_fn=lambda **kwargs: types.SimpleNamespace(
            model_dump=lambda mode="json": {
                "project_id": project.id,
                "new_tasks": [new_recovery_task.id],
            }
        ),
        persist_recovery_assignment_payload_fn=_persist_payload,
    )

    assert result.mutation_kind == "assignment"
    assert result.requires_replan is False
    assert result.patched_execution_plan is not None
    assert [b.batch_id for b in result.patched_execution_plan.execution_batches] == [
        "batch_1",
        "batch_1_patch_1",
        "batch_2",
    ]
    assert result.metadata["assigned_task_ids"] == [new_recovery_task.id]
    assert "recovery_assignment_input" in payload_artifacts
    assert "recovery_assignment_output" in payload_artifacts
    assert "recovery_assignment_compiled_plan" in payload_artifacts


def test_mutate_live_plan_assignment_can_escalate_to_replan(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(project_id=project.id, title="Current batch task")
    new_recovery_task = make_task(project_id=project.id, title="Recovery task")

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [current_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )
    batch = plan.execution_batches[0]

    resolved_intent = types.SimpleNamespace(
        intent_type="assign",
        legacy_action="continue_current_plan",
        remaining_plan_still_valid=True,
    )

    evaluation_decision = types.SimpleNamespace(
        decision="stage_incomplete",
        recommended_next_action="continue_current_plan",
        remaining_plan_still_valid=True,
    )

    recovery_context = types.SimpleNamespace(
        recovery_decisions=[],
        recovery_created_tasks=[],
    )

    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.call_recovery_assignment_model",
        lambda **kwargs: types.SimpleNamespace(
            model_dump=lambda mode="json": {
                "strategy": "requires_replan",
                "clusters": [{"cluster_id": "cluster_1"}],
            }
        ),
    )

    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.compile_recovery_assignment_plan",
        lambda **kwargs: types.SimpleNamespace(
            strategy="requires_replan",
            requires_replan=True,
            patched_execution_plan=None,
            assigned_task_ids=[],
            unassigned_task_ids=[new_recovery_task.id],
            compiled_cluster_assignments=[],
            notes=["Structural conflict detected."],
        ),
    )

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=evaluation_decision,
        recovery_context=recovery_context,
        created_recovery_task_ids=[new_recovery_task.id],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[],
        build_recovery_assignment_input_fn=lambda **kwargs: types.SimpleNamespace(
            model_dump=lambda mode="json": {"project_id": project.id}
        ),
        persist_recovery_assignment_payload_fn=lambda **kwargs: None,
    )

    assert result.mutation_kind == "escalated_to_replan"
    assert result.requires_replan is True
    assert result.patched_execution_plan is None
    assert result.metadata["unassigned_task_ids"] == [new_recovery_task.id]


def test_mutate_live_plan_resequence_patch_creates_patched_plan(
    db_session,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(project_id=project.id, title="Current task")
    blocking_recovery_task = make_task(project_id=project.id, title="Blocking recovery task")
    future_task = make_task(project_id=project.id, title="Future task")

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [current_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )
    batch = plan.execution_batches[0]

    resolved_intent = types.SimpleNamespace(
        intent_type="resequence",
        legacy_action="resequence_remaining_batches",
        remaining_plan_still_valid=True,
    )

    evaluation_decision = types.SimpleNamespace(
        new_recovery_tasks_blocking=True,
    )

    recovery_context = types.SimpleNamespace(
        recovery_decisions=[],
        recovery_created_tasks=[],
    )

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=evaluation_decision,
        recovery_context=recovery_context,
        created_recovery_task_ids=[blocking_recovery_task.id],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[],
        build_recovery_assignment_input_fn=lambda **kwargs: None,
        persist_recovery_assignment_payload_fn=lambda **kwargs: None,
    )

    assert result.mutation_kind == "resequence_patch"
    assert result.requires_replan is False
    assert result.patched_execution_plan is not None
    assert len(result.patched_execution_plan.execution_batches) == 3
    assert result.patched_execution_plan.execution_batches[1].task_ids == [blocking_recovery_task.id]


def test_mutate_live_plan_resequence_without_immediate_patch_returns_deferred(
    db_session,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(project_id=project.id, title="Current task")
    future_task = make_task(project_id=project.id, title="Future task")

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [current_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )
    batch = plan.execution_batches[0]

    resolved_intent = types.SimpleNamespace(
        intent_type="resequence",
        legacy_action="resequence_remaining_batches",
        remaining_plan_still_valid=True,
    )

    evaluation_decision = types.SimpleNamespace(
        new_recovery_tasks_blocking=False,
    )

    recovery_context = types.SimpleNamespace(
        recovery_decisions=[],
        recovery_created_tasks=[],
    )

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=evaluation_decision,
        recovery_context=recovery_context,
        created_recovery_task_ids=[],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[],
        build_recovery_assignment_input_fn=lambda **kwargs: None,
        persist_recovery_assignment_payload_fn=lambda **kwargs: None,
    )

    assert result.mutation_kind == "resequence_deferred"
    assert result.requires_replan is False
    assert result.patched_execution_plan is None

def test_mutate_live_plan_returns_resequence_deferred_without_patch(
    db_session,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(
        project_id=project.id,
        title="Current task",
        status=TASK_STATUS_PENDING,
    )
    future_task = make_task(
        project_id=project.id,
        title="Future task",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        plan_version=3,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [current_task.id],
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [future_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ],
    )
    batch = plan.execution_batches[0]

    resolved_intent = types.SimpleNamespace(
        intent_type="resequence",
        legacy_action="resequence_remaining_batches",
        remaining_plan_still_valid=True,
    )

    evaluation_decision = types.SimpleNamespace(
        new_recovery_tasks_blocking=False,
    )

    recovery_context = types.SimpleNamespace(
        recovery_decisions=[],
        recovery_created_tasks=[],
    )

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=evaluation_decision,
        recovery_context=recovery_context,
        created_recovery_task_ids=[],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[],
        build_recovery_assignment_input_fn=lambda **kwargs: None,
        persist_recovery_assignment_payload_fn=lambda **kwargs: None,
    )

    assert result.mutation_kind == "resequence_deferred"
    assert result.patched_execution_plan is None
    assert result.requires_replan is False


def test_mutate_live_plan_returns_escalated_to_replan_when_assignment_is_not_placeable(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(
        project_id=project.id,
        title="Current task",
        status=TASK_STATUS_PENDING,
    )
    recovery_task = make_task(
        project_id=project.id,
        title="Recovery task",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        plan_version=4,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [current_task.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            }
        ],
    )
    batch = plan.execution_batches[0]

    resolved_intent = types.SimpleNamespace(
        intent_type="assign",
        legacy_action="continue_current_plan",
        remaining_plan_still_valid=True,
    )

    evaluation_decision = types.SimpleNamespace(
        decision="stage_incomplete",
        recommended_next_action="continue_current_plan",
        remaining_plan_still_valid=True,
    )

    recovery_context = types.SimpleNamespace(
        recovery_decisions=[],
        recovery_created_tasks=[],
    )

    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.call_recovery_assignment_model",
        lambda **kwargs: types.SimpleNamespace(
            model_dump=lambda mode="json": {
                "strategy": "continue_with_assignment",
                "clusters": [{"cluster_id": "cluster_1"}],
            }
        ),
    )

    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.compile_recovery_assignment_plan",
        lambda **kwargs: types.SimpleNamespace(
            strategy="continue_with_assignment",
            requires_replan=True,
            patched_execution_plan=None,
            assigned_task_ids=[],
            unassigned_task_ids=[recovery_task.id],
            compiled_cluster_assignments=[],
            notes=["Structural conflict detected."],
        ),
    )

    persisted_payload_types = []

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=evaluation_decision,
        recovery_context=recovery_context,
        created_recovery_task_ids=[recovery_task.id],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[],
        build_recovery_assignment_input_fn=lambda **kwargs: types.SimpleNamespace(
            model_dump=lambda mode="json": {"project_id": project.id}
        ),
        persist_recovery_assignment_payload_fn=lambda **kwargs: persisted_payload_types.append(
            kwargs["artifact_type"]
        ),
    )

    assert result.mutation_kind == "escalated_to_replan"
    assert result.patched_execution_plan is None
    assert result.requires_replan is True
    assert result.metadata["unassigned_task_ids"] == [recovery_task.id]
    assert "recovery_assignment_input" in persisted_payload_types
    assert "recovery_assignment_output" in persisted_payload_types


def test_mutate_live_plan_returns_none_for_non_mutating_intent(
    db_session,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()

    current_task = make_task(
        project_id=project.id,
        title="Current task",
        status=TASK_STATUS_PENDING,
    )

    plan = make_execution_plan(
        plan_version=2,
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [current_task.id],
                "evaluation_focus": ["functional_coverage"],
            }
        ],
    )
    batch = plan.execution_batches[0]

    resolved_intent = types.SimpleNamespace(
        intent_type="continue",
        legacy_action="continue_current_plan",
        remaining_plan_still_valid=True,
    )

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=types.SimpleNamespace(),
        recovery_context=types.SimpleNamespace(
            recovery_decisions=[],
            recovery_created_tasks=[],
        ),
        created_recovery_task_ids=[],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[],
        build_recovery_assignment_input_fn=lambda **kwargs: None,
        persist_recovery_assignment_payload_fn=lambda **kwargs: None,
    )

    assert result.mutation_kind == "none"
    assert result.patched_execution_plan is None
    assert result.requires_replan is False