import json

from app.models.task import (
    EXECUTION_ENGINE,
    PENDING_ENGINE_ROUTING_EXECUTOR,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
)
from app.schemas.recovery import (
    RecoveryContext,
    RecoveryCreatedTaskRecord,
    RecoveryDecisionSummary,
    RecoveryOpenIssue,
)
from app.services.evaluation_service import (
    build_stage_evaluation_request,
    evaluate_checkpoint,
)


def test_build_stage_evaluation_request_includes_new_structured_summaries(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
):
    project = make_project(
        name="Evaluation Test Project",
        description="Project used to validate evaluation request building.",
    )

    executed_task = make_task(
        project_id=project.id,
        title="Executed task",
        description="Completed task included in the current checkpoint window.",
        status=TASK_STATUS_COMPLETED,
        executor_type=EXECUTION_ENGINE,
        sequence_order=1,
    )
    make_execution_run(
        task_id=executed_task.id,
        status="succeeded",
        work_summary="Executed task finished successfully.",
        work_details="Implemented the required scope for this batch.",
        completed_scope="Implemented the main behavior.",
        remaining_scope="No remaining scope.",
    )

    executed_artifact = make_artifact(
        project_id=project.id,
        task_id=executed_task.id,
        artifact_type="code_validation_result",
        content=json.dumps(
            {"decision": "completed", "summary": "Task validated successfully."}
        ),
    )

    pending_in_remaining_batch = make_task(
        project_id=project.id,
        title="Pending remaining batch task",
        description="Pending task already scheduled in a later batch.",
        status=TASK_STATUS_PENDING,
        executor_type=EXECUTION_ENGINE,
        sequence_order=2,
    )

    recovery_created_pending_task = make_task(
        project_id=project.id,
        title="Recovery-created follow-up task",
        description="Pending task created by recovery and not yet executed.",
        status=TASK_STATUS_PENDING,
        executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
        sequence_order=3,
    )

    unrelated_pending_task = make_task(
        project_id=project.id,
        title="Unrelated pending task",
        description="Another pending task outside the current checkpoint window.",
        status=TASK_STATUS_PENDING,
        executor_type=EXECUTION_ENGINE,
        sequence_order=4,
    )

    failed_task = make_task(
        project_id=project.id,
        title="Previously failed task",
        description="A failed task still relevant for project context.",
        status=TASK_STATUS_FAILED,
        executor_type=EXECUTION_ENGINE,
        sequence_order=5,
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "name": "Batch 1",
                "goal": "Execute the first task.",
                "task_ids": [executed_task.id],
                "evaluation_focus": ["functional_coverage"],
                "checkpoint_id": "cp_1",
                "checkpoint_reason": "Evaluate the first completed batch.",
            },
            {
                "batch_id": "batch_2",
                "name": "Batch 2",
                "goal": "Continue with pending work.",
                "task_ids": [pending_in_remaining_batch.id],
                "evaluation_focus": ["functional_coverage", "stage_closure"],
                "checkpoint_id": "cp_2",
                "checkpoint_reason": "Evaluate remaining work.",
            },
        ],
        plan_version=3,
        global_goal="Deliver the current stage successfully.",
        sequencing_rationale="The first batch establishes the validated baseline.",
    )

    recovery_context = RecoveryContext(
        recovery_decisions=[
            RecoveryDecisionSummary(
                source_task_id=failed_task.id,
                source_run_id=101,
                action="insert_followup",
                confidence="high",
                reason="A local follow-up task was created to recover missing scope.",
                still_blocks_progress=False,
                created_task_ids=[recovery_created_pending_task.id],
            )
        ],
        open_issues=[
            RecoveryOpenIssue(
                source_task_id=failed_task.id,
                source_run_id=101,
                issue_type="missing_scope",
                summary="A small portion of the expected behavior still needs implementation.",
                recommended_action="Execute the follow-up task in a later batch.",
            )
        ],
        recovery_created_tasks=[
            RecoveryCreatedTaskRecord(
                source_task_id=failed_task.id,
                source_run_id=101,
                created_task_id=recovery_created_pending_task.id,
                title=recovery_created_pending_task.title,
                planning_level=recovery_created_pending_task.planning_level,
                executor_type=recovery_created_pending_task.executor_type,
            )
        ],
    )
    assert isinstance(recovery_context, RecoveryContext)
    assert len(recovery_context.recovery_decisions) == 1
    assert len(recovery_context.open_issues) == 1
    assert len(recovery_context.recovery_created_tasks) == 1

    request = build_stage_evaluation_request(
        db=db_session,
        project_id=project.id,
        plan=plan,
        checkpoint_id="cp_1",
        executed_task_ids_since_last_checkpoint=[executed_task.id],
        checkpoint_artifact_window_ids=[executed_artifact.id],
        recovery_context=recovery_context,
    )

    assert request["project_name"] == project.name
    assert request["project_description"] == project.description

    recovery_tasks_created_summary = json.loads(request["recovery_tasks_created_summary"])
    assert recovery_tasks_created_summary["created_task_count"] == 1
    assert (
        recovery_tasks_created_summary["created_tasks"][0]["created_task_id"]
        == recovery_created_pending_task.id
    )
    assert (
        recovery_tasks_created_summary["created_tasks"][0]["source_task_id"]
        == failed_task.id
    )

    remaining_batches_summary = json.loads(request["remaining_batches_summary"])
    assert remaining_batches_summary["remaining_batch_count"] == 1
    assert remaining_batches_summary["remaining_batches"][0]["batch_id"] == "batch_2"
    assert remaining_batches_summary["remaining_batches"][0]["task_ids"] == [
        pending_in_remaining_batch.id
    ]

    pending_task_summary = json.loads(request["pending_task_summary"])
    pending_task_ids = {item["task_id"] for item in pending_task_summary["pending_tasks"]}
    assert pending_in_remaining_batch.id in pending_task_ids
    assert recovery_created_pending_task.id in pending_task_ids
    assert unrelated_pending_task.id in pending_task_ids
    assert executed_task.id not in pending_task_ids

    pending_by_id = {
        item["task_id"]: item for item in pending_task_summary["pending_tasks"]
    }
    assert pending_by_id[pending_in_remaining_batch.id]["is_in_remaining_batches"] is True
    assert pending_by_id[pending_in_remaining_batch.id]["is_recovery_generated"] is False

    assert pending_by_id[recovery_created_pending_task.id]["is_in_remaining_batches"] is False
    assert pending_by_id[recovery_created_pending_task.id]["is_recovery_generated"] is True

    checkpoint_artifact_window_summary = json.loads(
        request["checkpoint_artifact_window_summary"]
    )
    assert checkpoint_artifact_window_summary["artifact_count"] == 1
    assert (
        checkpoint_artifact_window_summary["artifacts"][0]["artifact_id"]
        == executed_artifact.id
    )
    assert (
        checkpoint_artifact_window_summary["artifacts"][0]["artifact_type"]
        == "code_validation_result"
    )

    processed_batch_summary = json.loads(request["processed_batch_summary"])
    assert processed_batch_summary["evaluated_batch"]["batch_id"] == "batch_1"
    assert processed_batch_summary["executed_task_count"] == 1
    assert processed_batch_summary["executed_tasks"][0]["task_id"] == executed_task.id

    task_state_summary = json.loads(request["task_state_summary"])
    assert len(task_state_summary["evaluated_tasks"]) == 1
    assert task_state_summary["evaluated_tasks"][0]["task_id"] == executed_task.id
    assert task_state_summary["evaluated_tasks"][0]["latest_run"]["status"] == "succeeded"

    recovery_context_summary = json.loads(request["recovery_context_summary"])
    assert len(recovery_context_summary["recovery_created_tasks"]) == 1
    assert (
        recovery_context_summary["recovery_created_tasks"][0]["created_task_id"]
        == recovery_created_pending_task.id
    )

    additional_context = json.loads(request["additional_context"])
    assert additional_context["project"]["project_id"] == project.id
    assert additional_context["checkpoint_window"]["executed_task_ids"] == [executed_task.id]
    assert additional_context["checkpoint_window"]["artifact_ids"] == [executed_artifact.id]
    assert additional_context["next_batch"]["batch_id"] == "batch_2"
    assert (
        recovery_created_pending_task.id
        in additional_context["recovery_summary"]["created_task_ids"]
    )


def test_evaluate_checkpoint_passes_structured_request_to_model(
    db_session,
    monkeypatch,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
    make_execution_plan,
    make_stage_evaluation_output,
):
    project = make_project(
        name="Checkpoint Call Project",
        description="Project used to verify the evaluation model call.",
    )

    executed_task = make_task(
        project_id=project.id,
        title="Executed task",
        status=TASK_STATUS_COMPLETED,
        sequence_order=1,
    )
    make_execution_run(
        task_id=executed_task.id,
        status="succeeded",
        work_summary="Task executed correctly.",
    )

    artifact = make_artifact(
        project_id=project.id,
        task_id=executed_task.id,
        artifact_type="code_validation_result",
        content=json.dumps({"decision": "completed"}),
    )

    pending_task = make_task(
        project_id=project.id,
        title="Pending next task",
        status=TASK_STATUS_PENDING,
        sequence_order=2,
    )

    plan = make_execution_plan(
        batches=[
            {
                "batch_id": "batch_1",
                "task_ids": [executed_task.id],
                "checkpoint_id": "cp_1",
                "evaluation_focus": ["functional_coverage"],
            },
            {
                "batch_id": "batch_2",
                "task_ids": [pending_task.id],
                "checkpoint_id": "cp_2",
                "evaluation_focus": ["functional_coverage", "stage_closure"],
            },
        ]
    )

    expected_output = make_stage_evaluation_output(
        decision="stage_incomplete",
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The current plan still represents the correct next work.",
        decision_signals=["remaining_plan_still_valid"],
        plan_change_scope="none",
        remaining_plan_still_valid=True,
    )

    captured_kwargs = {}

    def _fake_call_stage_evaluation_model(**kwargs):
        captured_kwargs.update(kwargs)
        return expected_output

    monkeypatch.setattr(
        "app.services.evaluation_service.call_stage_evaluation_model",
        _fake_call_stage_evaluation_model,
    )

    result = evaluate_checkpoint(
        db=db_session,
        project_id=project.id,
        plan=plan,
        checkpoint_id="cp_1",
        executed_task_ids_since_last_checkpoint=[executed_task.id],
        checkpoint_artifact_window_ids=[artifact.id],
        recovery_context=RecoveryContext(),
    )

    assert result == expected_output

    assert captured_kwargs["project_name"] == project.name
    assert captured_kwargs["project_description"] == project.description

    for key in (
        "stage_goal",
        "stage_scope_summary",
        "processed_batch_summary",
        "task_state_summary",
        "recovery_context_summary",
        "recovery_tasks_created_summary",
        "remaining_batches_summary",
        "pending_task_summary",
        "checkpoint_artifact_window_summary",
        "additional_context",
    ):
        assert key in captured_kwargs
        assert isinstance(captured_kwargs[key], str)
        assert captured_kwargs[key].strip() != ""

    remaining_batches_summary = json.loads(captured_kwargs["remaining_batches_summary"])
    assert remaining_batches_summary["remaining_batch_count"] == 1
    assert remaining_batches_summary["remaining_batches"][0]["batch_id"] == "batch_2"

    pending_task_summary = json.loads(captured_kwargs["pending_task_summary"])
    assert any(item["task_id"] == pending_task.id for item in pending_task_summary["pending_tasks"])

    checkpoint_artifact_window_summary = json.loads(
        captured_kwargs["checkpoint_artifact_window_summary"]
    )
    assert checkpoint_artifact_window_summary["artifact_count"] == 1
    assert checkpoint_artifact_window_summary["artifacts"][0]["artifact_id"] == artifact.id