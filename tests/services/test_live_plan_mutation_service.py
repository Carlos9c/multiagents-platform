import pytest

from app.schemas.post_batch import PostBatchTaskRunSummary
from app.schemas.post_batch_intent import ResolvedPostBatchIntent
from app.schemas.recovery import RecoveryContext
from app.services.live_plan_mutation_service import (
    LivePlanMutationServiceError,
    mutate_live_plan,
)


def _build_resolved_intent(
    *,
    intent_type: str,
    mutation_scope: str,
    remaining_plan_still_valid: bool = True,
    has_new_recovery_tasks: bool = False,
    requires_plan_mutation: bool = False,
    requires_all_new_tasks_assigned: bool = False,
    can_continue_after_application: bool = False,
    should_close_stage: bool = False,
    requires_manual_review: bool = False,
    reopened_finalization: bool = False,
    notes: str = "Resolved intent for testing.",
    decision_signals: list[str] | None = None,
) -> ResolvedPostBatchIntent:
    return ResolvedPostBatchIntent(
        intent_type=intent_type,
        mutation_scope=mutation_scope,
        remaining_plan_still_valid=remaining_plan_still_valid,
        has_new_recovery_tasks=has_new_recovery_tasks,
        requires_plan_mutation=requires_plan_mutation,
        requires_all_new_tasks_assigned=requires_all_new_tasks_assigned,
        can_continue_after_application=can_continue_after_application,
        should_close_stage=should_close_stage,
        requires_manual_review=requires_manual_review,
        reopened_finalization=reopened_finalization,
        notes=notes,
        decision_signals=decision_signals or [],
    )


def _build_task_run_summary(
    *,
    task_id: int,
    run_id: int,
    run_status: str = "succeeded|task:completed",
) -> PostBatchTaskRunSummary:
    return PostBatchTaskRunSummary(
        task_id=task_id,
        run_id=run_id,
        run_status=run_status,
    )


class DummyMutationResult:
    def __init__(self, **kwargs):
        self.mutation_kind = kwargs.get("mutation_kind", "assignment")
        self.patched_execution_plan = kwargs.get("patched_execution_plan")
        self.requires_replan = kwargs.get("requires_replan", False)
        self.notes = kwargs.get("notes", [])
        self.metadata = kwargs.get("metadata", {})


def test_mutate_live_plan_builds_assignment_input_with_canonical_intent_fields(
    db_session,
    make_project,
    make_task,
    make_execution_plan,
    monkeypatch,
):
    project = make_project()
    current_task = make_task(project_id=project.id, title="Current batch task")
    next_task = make_task(project_id=project.id, title="Next batch task")
    recovery_task = make_task(project_id=project.id, title="Recovery task")

    plan = make_execution_plan(
        plan_version=3,
        batches=[
            {
                "batch_id": "plan_3_batch_1",
                "batch_internal_id": "3_1",
                "batch_index": 1,
                "plan_version": 3,
                "task_ids": [current_task.id],
            },
            {
                "batch_id": "plan_3_batch_2",
                "batch_internal_id": "3_2",
                "batch_index": 2,
                "plan_version": 3,
                "task_ids": [next_task.id],
            },
        ],
    )
    batch = plan.execution_batches[0]

    resolved_intent = _build_resolved_intent(
        intent_type="assign",
        mutation_scope="assignment",
        has_new_recovery_tasks=True,
        requires_plan_mutation=True,
        requires_all_new_tasks_assigned=True,
        can_continue_after_application=True,
        reopened_finalization=False,
    )

    captured = {}

    class DummyAssignmentInput:
        def __init__(self, **kwargs):
            captured["assignment_input_kwargs"] = kwargs
            self._payload = kwargs

        def model_dump(self, mode="json"):
            return dict(self._payload)

    class DummyAssignmentOutput:
        strategy = "continue_with_assignment"

        def model_dump(self, mode="json"):
            return {"strategy": self.strategy}

    class DummyCompiledAssignment:
        strategy = "continue_with_assignment"
        requires_replan = False
        assigned_task_ids = [recovery_task.id]
        unassigned_task_ids = []
        notes = ["compiled"]
        compiled_cluster_assignments = []
        patched_execution_plan = plan

    def fake_build_recovery_assignment_input_fn(**kwargs):
        return DummyAssignmentInput(**kwargs)

    persisted_payloads = []

    def fake_persist_recovery_assignment_payload_fn(**kwargs):
        persisted_payloads.append(kwargs)
        return None

    def fake_call_recovery_assignment_model(*, assignment_input):
        assert assignment_input is not None
        return DummyAssignmentOutput()

    def fake_compile_recovery_assignment_plan(*, plan, assignment_input, assignment_output):
        assert assignment_input is not None
        assert assignment_output is not None
        return DummyCompiledAssignment()

    def fake_persist_patched_execution_plan(**kwargs):
        captured["persisted_plan"] = kwargs["plan"]

    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.call_recovery_assignment_model",
        fake_call_recovery_assignment_model,
    )
    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.compile_recovery_assignment_plan",
        fake_compile_recovery_assignment_plan,
    )
    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.persist_patched_execution_plan",
        fake_persist_patched_execution_plan,
    )

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=type(
            "EvalDecision",
            (),
            {
                "new_recovery_tasks_blocking": False,
            },
        )(),
        recovery_context=RecoveryContext(),
        created_recovery_task_ids=[recovery_task.id],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[
            _build_task_run_summary(task_id=current_task.id, run_id=501)
        ],
        build_recovery_assignment_input_fn=fake_build_recovery_assignment_input_fn,
        persist_recovery_assignment_payload_fn=fake_persist_recovery_assignment_payload_fn,
    )

    assert captured["assignment_input_kwargs"]["resolved_intent_type"] == "assign"
    assert captured["assignment_input_kwargs"]["resolved_mutation_scope"] == "assignment"
    assert "resolved_action" not in captured["assignment_input_kwargs"]

    assert result.mutation_kind == "assignment"
    assert result.requires_replan is False
    assert result.patched_execution_plan == plan
    assert result.metadata["assigned_task_ids"] == [recovery_task.id]
    assert persisted_payloads[0]["artifact_type"] == "recovery_assignment_input"
    assert persisted_payloads[1]["artifact_type"] == "recovery_assignment_output"
    assert persisted_payloads[2]["artifact_type"] == "recovery_assignment_compiled_plan"


def test_resolved_post_batch_intent_rejects_assign_with_non_assignment_scope():
    with pytest.raises(ValueError) as exc_info:
        _build_resolved_intent(
            intent_type="assign",
            mutation_scope="resequence",
            has_new_recovery_tasks=True,
            requires_plan_mutation=True,
            requires_all_new_tasks_assigned=True,
            can_continue_after_application=True,
        )

    assert "intent_type='assign' requires mutation_scope='assignment'" in str(exc_info.value)


def test_resolved_post_batch_intent_rejects_resequence_with_non_resequence_scope():
    with pytest.raises(ValueError) as exc_info:
        _build_resolved_intent(
            intent_type="resequence",
            mutation_scope="assignment",
            remaining_plan_still_valid=True,
            has_new_recovery_tasks=True,
            requires_plan_mutation=True,
            requires_all_new_tasks_assigned=True,
            can_continue_after_application=False,
            reopened_finalization=True,
        )

    assert "intent_type='resequence' requires mutation_scope='resequence'" in str(exc_info.value)


def test_mutate_live_plan_returns_escalated_to_replan_when_compiler_requires_replan(
    db_session,
    make_project,
    make_task,
    make_execution_plan,
    monkeypatch,
):
    project = make_project()
    current_task = make_task(project_id=project.id, title="Current batch task")
    recovery_task = make_task(project_id=project.id, title="Recovery task")

    plan = make_execution_plan(
        plan_version=3,
        batches=[
            {
                "batch_id": "plan_3_batch_1",
                "batch_internal_id": "3_1",
                "batch_index": 1,
                "plan_version": 3,
                "task_ids": [current_task.id],
            }
        ],
    )
    batch = plan.execution_batches[0]

    resolved_intent = _build_resolved_intent(
        intent_type="assign",
        mutation_scope="assignment",
        has_new_recovery_tasks=True,
        requires_plan_mutation=True,
        requires_all_new_tasks_assigned=True,
        can_continue_after_application=True,
    )

    class DummyAssignmentInput:
        def model_dump(self, mode="json"):
            return {"ok": True}

    class DummyAssignmentOutput:
        strategy = "requires_replan"

        def model_dump(self, mode="json"):
            return {"strategy": self.strategy}

    class DummyCompiledAssignment:
        strategy = "requires_replan"
        requires_replan = True
        assigned_task_ids = []
        unassigned_task_ids = [recovery_task.id]
        notes = ["structural conflict"]
        compiled_cluster_assignments = []
        patched_execution_plan = None

    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.call_recovery_assignment_model",
        lambda *, assignment_input: DummyAssignmentOutput(),
    )
    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.compile_recovery_assignment_plan",
        lambda *, plan, assignment_input, assignment_output: DummyCompiledAssignment(),
    )
    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.persist_patched_execution_plan",
        lambda **kwargs: None,
    )

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=type("EvalDecision", (), {"new_recovery_tasks_blocking": False})(),
        recovery_context=RecoveryContext(),
        created_recovery_task_ids=[recovery_task.id],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[
            _build_task_run_summary(task_id=current_task.id, run_id=501)
        ],
        build_recovery_assignment_input_fn=lambda **kwargs: DummyAssignmentInput(),
        persist_recovery_assignment_payload_fn=lambda **kwargs: None,
    )

    assert result.mutation_kind == "escalated_to_replan"
    assert result.requires_replan is True
    assert result.patched_execution_plan is None
    assert result.metadata["assigned_task_ids"] == []
    assert result.metadata["unassigned_task_ids"] == [recovery_task.id]


def test_mutate_live_plan_resequence_inserts_patch_batch_when_blocking_recovery_tasks_exist(
    db_session,
    make_project,
    make_task,
    make_execution_plan,
    monkeypatch,
):
    project = make_project()
    current_task = make_task(project_id=project.id, title="Current batch task")
    next_task = make_task(project_id=project.id, title="Next batch task")
    recovery_task = make_task(project_id=project.id, title="Recovery patch task")

    plan = make_execution_plan(
        plan_version=4,
        batches=[
            {
                "batch_id": "plan_4_batch_1",
                "batch_internal_id": "4_1",
                "batch_index": 1,
                "plan_version": 4,
                "task_ids": [current_task.id],
            },
            {
                "batch_id": "plan_4_batch_2",
                "batch_internal_id": "4_2",
                "batch_index": 2,
                "plan_version": 4,
                "task_ids": [next_task.id],
            },
        ],
    )
    batch = plan.execution_batches[0]

    resolved_intent = _build_resolved_intent(
        intent_type="resequence",
        mutation_scope="resequence",
        remaining_plan_still_valid=True,
        has_new_recovery_tasks=True,
        requires_plan_mutation=True,
        requires_all_new_tasks_assigned=True,
        can_continue_after_application=False,
        reopened_finalization=True,
    )

    persisted = {}

    def fake_persist_patched_execution_plan(**kwargs):
        persisted["plan"] = kwargs["plan"]

    monkeypatch.setattr(
        "app.services.live_plan_mutation_service.persist_patched_execution_plan",
        fake_persist_patched_execution_plan,
    )

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=type("EvalDecision", (), {"new_recovery_tasks_blocking": True})(),
        recovery_context=RecoveryContext(),
        created_recovery_task_ids=[recovery_task.id],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[
            _build_task_run_summary(task_id=current_task.id, run_id=601)
        ],
        build_recovery_assignment_input_fn=lambda **kwargs: None,
        persist_recovery_assignment_payload_fn=lambda **kwargs: None,
    )

    assert result.mutation_kind == "resequence_patch"
    assert result.requires_replan is False
    assert result.patched_execution_plan is not None
    assert result.metadata["patched_task_ids"] == [recovery_task.id]
    assert result.metadata["anchor_batch_id"] == batch.batch_id

    patched_batches = result.patched_execution_plan.execution_batches
    assert len(patched_batches) == 3
    assert patched_batches[1].is_patch_batch is True
    assert patched_batches[1].task_ids == [recovery_task.id]
    assert persisted["plan"] == result.patched_execution_plan


def test_mutate_live_plan_resequence_returns_deferred_when_no_immediate_patch_applies(
    db_session,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()
    current_task = make_task(project_id=project.id, title="Current batch task")
    recovery_task = make_task(project_id=project.id, title="Recovery task")

    plan = make_execution_plan(
        plan_version=4,
        batches=[
            {
                "batch_id": "plan_4_batch_1",
                "batch_internal_id": "4_1",
                "batch_index": 1,
                "plan_version": 4,
                "task_ids": [current_task.id],
            }
        ],
    )
    batch = plan.execution_batches[0]

    resolved_intent = _build_resolved_intent(
        intent_type="resequence",
        mutation_scope="resequence",
        remaining_plan_still_valid=True,
        has_new_recovery_tasks=True,
        requires_plan_mutation=True,
        requires_all_new_tasks_assigned=True,
        can_continue_after_application=False,
        reopened_finalization=True,
    )

    result = mutate_live_plan(
        db=db_session,
        project=project,
        plan=plan,
        batch=batch,
        resolved_intent=resolved_intent,
        evaluation_decision=type("EvalDecision", (), {"new_recovery_tasks_blocking": False})(),
        recovery_context=RecoveryContext(),
        created_recovery_task_ids=[recovery_task.id],
        executed_task_ids=[current_task.id],
        successful_task_ids=[current_task.id],
        problematic_run_ids=[],
        task_run_summaries=[
            _build_task_run_summary(task_id=current_task.id, run_id=601)
        ],
        build_recovery_assignment_input_fn=lambda **kwargs: None,
        persist_recovery_assignment_payload_fn=lambda **kwargs: None,
    )

    assert result.mutation_kind == "resequence_deferred"
    assert result.requires_replan is False
    assert result.patched_execution_plan is None
    assert result.metadata["patched_task_ids"] == []
    assert result.metadata["anchor_batch_id"] == batch.batch_id

def test_mutate_live_plan_returns_none_for_non_mutating_intents(
    db_session,
    make_project,
    make_task,
    make_execution_plan,
):
    project = make_project()
    current_task = make_task(project_id=project.id, title="Current batch task")

    plan = make_execution_plan(
        plan_version=5,
        batches=[
            {
                "batch_id": "plan_5_batch_1",
                "batch_internal_id": "5_1",
                "batch_index": 1,
                "plan_version": 5,
                "task_ids": [current_task.id],
            }
        ],
    )
    batch = plan.execution_batches[0]

    for intent_type in ("continue", "manual_review", "close", "replan"):
        if intent_type == "continue":
            resolved_intent = _build_resolved_intent(
                intent_type="continue",
                mutation_scope="none",
                requires_plan_mutation=False,
                can_continue_after_application=True,
            )
        elif intent_type == "manual_review":
            resolved_intent = _build_resolved_intent(
                intent_type="manual_review",
                mutation_scope="none",
                requires_plan_mutation=False,
                requires_manual_review=True,
                can_continue_after_application=False,
            )
        elif intent_type == "close":
            resolved_intent = _build_resolved_intent(
                intent_type="close",
                mutation_scope="none",
                requires_plan_mutation=False,
                should_close_stage=True,
                can_continue_after_application=False,
            )
        else:
            resolved_intent = _build_resolved_intent(
                intent_type="replan",
                mutation_scope="replan",
                remaining_plan_still_valid=False,
                requires_plan_mutation=True,
                reopened_finalization=True,
                can_continue_after_application=False,
            )

        result = mutate_live_plan(
            db=db_session,
            project=project,
            plan=plan,
            batch=batch,
            resolved_intent=resolved_intent,
            evaluation_decision=type("EvalDecision", (), {"new_recovery_tasks_blocking": False})(),
            recovery_context=RecoveryContext(),
            created_recovery_task_ids=[],
            executed_task_ids=[current_task.id],
            successful_task_ids=[current_task.id],
            problematic_run_ids=[],
            task_run_summaries=[
                _build_task_run_summary(task_id=current_task.id, run_id=701)
            ],
            build_recovery_assignment_input_fn=lambda **kwargs: None,
            persist_recovery_assignment_payload_fn=lambda **kwargs: None,
        )

        assert result.mutation_kind == "none"
        assert result.requires_replan is False
        assert result.patched_execution_plan is None
        assert result.metadata == {}