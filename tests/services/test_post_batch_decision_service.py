from app.services.post_batch_decision_service import (
    build_post_batch_decision_signals,
    resolve_post_batch_intent,
)


class DummyEvaluationDecision:
    def __init__(self, **kwargs):
        self.decision = kwargs.get("decision", "stage_incomplete")
        self.decision_summary = kwargs.get(
            "decision_summary",
            "Stage is incomplete and the workflow should continue.",
        )
        self.project_stage_closed = kwargs.get("project_stage_closed", False)
        self.manual_review_required = kwargs.get("manual_review_required", False)
        self.remaining_plan_still_valid = kwargs.get("remaining_plan_still_valid", True)
        self.plan_change_scope = kwargs.get("plan_change_scope", "none")
        self.recommended_next_action = kwargs.get("recommended_next_action", None)
        self.recommended_next_action_reason = kwargs.get(
            "recommended_next_action_reason",
            None,
        )
        self.followup_atomic_tasks_required = kwargs.get(
            "followup_atomic_tasks_required",
            False,
        )
        self.recovery_strategy = kwargs.get("recovery_strategy", "none")
        self.new_recovery_tasks_blocking = kwargs.get(
            "new_recovery_tasks_blocking",
            None,
        )
        self.single_task_tail_risk = kwargs.get("single_task_tail_risk", False)
        self.key_risks = kwargs.get("key_risks", [])
        self.notes = kwargs.get("notes", [])
        self.decision_signals = kwargs.get("decision_signals", [])
        self.replan = kwargs.get(
            "replan",
            type(
                "ReplanInstruction",
                (),
                {
                    "required": kwargs.get("replan_required", False),
                    "level": kwargs.get("replan_level", ""),
                },
            )(),
        )


class DummyRecoveryTask:
    def __init__(self, created_task_id: int, source_task_id: int = 1, source_run_id: int = 1):
        self.created_task_id = created_task_id
        self.source_task_id = source_task_id
        self.source_run_id = source_run_id


class DummyRecoveryContext:
    def __init__(self, created_task_ids: list[int] | None = None):
        self.recovery_created_tasks = [
            DummyRecoveryTask(created_task_id=task_id)
            for task_id in (created_task_ids or [])
        ]


def _resolve(
    *,
    evaluation_decision: DummyEvaluationDecision,
    recovery_context: DummyRecoveryContext | None = None,
    has_pending_valid_tasks: bool,
    remaining_batch_count: int,
    is_final_batch: bool,
    has_preexisting_pending_valid_tasks: bool = False,
    preexisting_pending_valid_task_count: int = 0,
    has_new_recovery_pending_tasks: bool = False,
    new_recovery_pending_task_count: int = 0,
):
    signals = build_post_batch_decision_signals(
        evaluation_decision=evaluation_decision,
        recovery_context=recovery_context or DummyRecoveryContext(),
        has_pending_valid_tasks=has_pending_valid_tasks,
        remaining_batch_count=remaining_batch_count,
        is_final_batch=is_final_batch,
    )
    signals.has_preexisting_pending_valid_tasks = has_preexisting_pending_valid_tasks
    signals.preexisting_pending_valid_task_count = preexisting_pending_valid_task_count
    signals.has_new_recovery_pending_tasks = has_new_recovery_pending_tasks
    signals.new_recovery_pending_task_count = new_recovery_pending_task_count
    return resolve_post_batch_intent(signals)


def test_resolve_post_batch_intent_returns_continue_for_stable_plan_without_new_work():
    resolved = _resolve(
        evaluation_decision=DummyEvaluationDecision(
            recommended_next_action="continue_current_plan",
            plan_change_scope="none",
            remaining_plan_still_valid=True,
        ),
        has_pending_valid_tasks=True,
        remaining_batch_count=2,
        is_final_batch=False,
    )

    assert resolved.intent_type == "continue"
    assert resolved.mutation_scope == "none"
    assert resolved.requires_plan_mutation is False
    assert resolved.can_continue_after_application is True
    assert resolved.requires_manual_review is False
    assert resolved.should_close_stage is False
    assert resolved.reopened_finalization is False


def test_resolve_post_batch_intent_returns_manual_review_when_explicitly_required():
    resolved = _resolve(
        evaluation_decision=DummyEvaluationDecision(
            decision="manual_review_required",
            manual_review_required=True,
            recommended_next_action="manual_review",
        ),
        has_pending_valid_tasks=True,
        remaining_batch_count=1,
        is_final_batch=False,
    )

    assert resolved.intent_type == "manual_review"
    assert resolved.mutation_scope == "none"
    assert resolved.requires_plan_mutation is False
    assert resolved.can_continue_after_application is False
    assert resolved.requires_manual_review is True
    assert resolved.should_close_stage is False
    assert resolved.reopened_finalization is False


def test_resolve_post_batch_intent_returns_replan_for_structural_invalidation():
    resolved = _resolve(
        evaluation_decision=DummyEvaluationDecision(
            recommended_next_action="replan_remaining_work",
            remaining_plan_still_valid=False,
            replan_required=True,
            replan_level="high_level",
            plan_change_scope="high_level_replan",
        ),
        has_pending_valid_tasks=True,
        remaining_batch_count=2,
        is_final_batch=False,
    )

    assert resolved.intent_type == "replan"
    assert resolved.mutation_scope == "replan"
    assert resolved.remaining_plan_still_valid is False
    assert resolved.requires_plan_mutation is True
    assert resolved.can_continue_after_application is False
    assert resolved.requires_manual_review is False
    assert resolved.reopened_finalization is True


def test_resolve_post_batch_intent_returns_resequence_for_local_reordering():
    resolved = _resolve(
        evaluation_decision=DummyEvaluationDecision(
            recommended_next_action="resequence_remaining_batches",
            plan_change_scope="local_resequencing",
            remaining_plan_still_valid=True,
            followup_atomic_tasks_required=True,
            new_recovery_tasks_blocking=True,
        ),
        recovery_context=DummyRecoveryContext(created_task_ids=[101]),
        has_pending_valid_tasks=True,
        remaining_batch_count=2,
        is_final_batch=False,
        has_new_recovery_pending_tasks=True,
        new_recovery_pending_task_count=1,
    )

    assert resolved.intent_type == "resequence"
    assert resolved.mutation_scope == "resequence"
    assert resolved.requires_plan_mutation is True
    assert resolved.requires_all_new_tasks_assigned is True
    assert resolved.can_continue_after_application is False
    assert resolved.reopened_finalization is True


def test_resolve_post_batch_intent_returns_assign_when_new_recovery_work_can_be_integrated():
    resolved = _resolve(
        evaluation_decision=DummyEvaluationDecision(
            recommended_next_action="continue_current_plan",
            plan_change_scope="none",
            remaining_plan_still_valid=True,
        ),
        recovery_context=DummyRecoveryContext(created_task_ids=[101, 102]),
        has_pending_valid_tasks=True,
        remaining_batch_count=2,
        is_final_batch=False,
        has_preexisting_pending_valid_tasks=True,
        preexisting_pending_valid_task_count=3,
        has_new_recovery_pending_tasks=True,
        new_recovery_pending_task_count=2,
    )

    assert resolved.intent_type == "assign"
    assert resolved.mutation_scope == "assignment"
    assert resolved.requires_plan_mutation is True
    assert resolved.has_new_recovery_tasks is True
    assert resolved.requires_all_new_tasks_assigned is True
    assert resolved.can_continue_after_application is True
    assert resolved.requires_manual_review is False


def test_resolve_post_batch_intent_returns_close_only_for_final_batch_without_new_work():
    resolved = _resolve(
        evaluation_decision=DummyEvaluationDecision(
            decision="stage_completed",
            project_stage_closed=True,
            recommended_next_action="close_stage",
            remaining_plan_still_valid=True,
        ),
        has_pending_valid_tasks=False,
        remaining_batch_count=0,
        is_final_batch=True,
    )

    assert resolved.intent_type == "close"
    assert resolved.mutation_scope == "none"
    assert resolved.requires_plan_mutation is False
    assert resolved.should_close_stage is True
    assert resolved.requires_manual_review is False
    assert resolved.reopened_finalization is False


def test_resolve_post_batch_intent_does_not_close_stage_when_new_recovery_work_exists():
    resolved = _resolve(
        evaluation_decision=DummyEvaluationDecision(
            decision="stage_completed",
            project_stage_closed=True,
            recommended_next_action="close_stage",
            remaining_plan_still_valid=True,
        ),
        recovery_context=DummyRecoveryContext(created_task_ids=[101]),
        has_pending_valid_tasks=True,
        remaining_batch_count=0,
        is_final_batch=True,
        has_new_recovery_pending_tasks=True,
        new_recovery_pending_task_count=1,
    )

    assert resolved.intent_type != "close"
    assert resolved.should_close_stage is False


def test_resolve_post_batch_intent_falls_back_to_manual_review_when_signals_are_ambiguous():
    resolved = _resolve(
        evaluation_decision=DummyEvaluationDecision(
            recommended_next_action=None,
            plan_change_scope="none",
            remaining_plan_still_valid=True,
        ),
        has_pending_valid_tasks=True,
        remaining_batch_count=1,
        is_final_batch=False,
    )

    assert resolved.intent_type == "manual_review"
    assert resolved.requires_manual_review is True
    assert "manual_review_fallback" in resolved.decision_signals