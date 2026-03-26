import pytest

from app.services.post_batch_decision_service import (
    PostBatchDecisionSignals,
    build_post_batch_decision_signals,
    resolve_post_batch_decision,
)


class DummyReplan:
    def __init__(self, required=False, level=""):
        self.required = required
        self.level = level


class DummyEvaluationDecision:
    def __init__(self, **kwargs):
        self.decision = kwargs.get("decision", "stage_incomplete")
        self.decision_summary = kwargs.get(
            "decision_summary",
            "Checkpoint evaluation completed.",
        )
        self.project_stage_closed = kwargs.get("project_stage_closed", False)
        self.manual_review_required = kwargs.get("manual_review_required", False)
        self.remaining_plan_still_valid = kwargs.get("remaining_plan_still_valid", True)
        self.plan_change_scope = kwargs.get("plan_change_scope", "none")
        self.recommended_next_action = kwargs.get(
            "recommended_next_action",
            "continue_current_plan",
        )
        self.recommended_next_action_reason = kwargs.get(
            "recommended_next_action_reason",
            "The current plan can continue.",
        )
        self.replan = kwargs.get("replan", DummyReplan(required=False, level=""))
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


class DummyRecoveryContext:
    def __init__(self, recovery_created_tasks=None):
        self.recovery_created_tasks = recovery_created_tasks or []


def make_signals(**overrides) -> PostBatchDecisionSignals:
    base = dict(
        decision="stage_incomplete",
        decision_summary="Checkpoint evaluation completed.",
        project_stage_closed=False,
        manual_review_required=False,
        remaining_plan_still_valid=True,
        plan_change_scope="none",
        recommended_next_action="continue_current_plan",
        recommended_next_action_reason="The remaining plan can continue.",
        replan_required=False,
        replan_level="",
        followup_atomic_tasks_required=False,
        recovery_strategy="none",
        new_recovery_tasks_created=False,
        new_recovery_tasks_blocking=None,
        single_task_tail_risk=False,
        has_pending_valid_tasks=False,
        remaining_batch_count=1,
        is_final_batch=False,
        has_preexisting_pending_valid_tasks=False,
        preexisting_pending_valid_task_count=0,
        has_new_recovery_pending_tasks=False,
        new_recovery_pending_task_count=0,
        key_risks=[],
        notes=[],
        decision_signals=[],
    )
    base.update(overrides)
    return PostBatchDecisionSignals(**base)


def test_build_post_batch_decision_signals_reads_new_evaluator_fields():
    evaluation = DummyEvaluationDecision(
        decision="stage_incomplete",
        decision_summary="The batch completed but new work exists.",
        project_stage_closed=False,
        manual_review_required=False,
        remaining_plan_still_valid=True,
        plan_change_scope="local_resequencing",
        recommended_next_action="resequence_remaining_batches",
        recommended_next_action_reason="New work must run before continuing.",
        replan=DummyReplan(required=False, level=""),
        followup_atomic_tasks_required=True,
        recovery_strategy="insert_followup_atomic_tasks",
        new_recovery_tasks_blocking=True,
        single_task_tail_risk=False,
        key_risks=["ordering_change"],
        notes=["Need local patching."],
        decision_signals=["new_work_requires_precedence"],
    )
    recovery = DummyRecoveryContext(recovery_created_tasks=[{"task_id": 101}])

    signals = build_post_batch_decision_signals(
        evaluation_decision=evaluation,
        recovery_context=recovery,
        has_pending_valid_tasks=True,
        remaining_batch_count=2,
        is_final_batch=False,
    )

    assert signals.decision == "stage_incomplete"
    assert signals.remaining_plan_still_valid is True
    assert signals.plan_change_scope == "local_resequencing"
    assert signals.recommended_next_action == "resequence_remaining_batches"
    assert signals.followup_atomic_tasks_required is True
    assert signals.recovery_strategy == "insert_followup_atomic_tasks"
    assert signals.new_recovery_tasks_created is True
    assert signals.new_recovery_tasks_blocking is True
    assert signals.remaining_batch_count == 2
    assert signals.key_risks == ["ordering_change"]
    assert signals.notes == ["Need local patching."]
    assert signals.decision_signals == ["new_work_requires_precedence"]


def test_resolve_post_batch_decision_closes_stage_when_stage_completed():
    signals = make_signals(
        decision="stage_completed",
        project_stage_closed=True,
        recommended_next_action="close_stage",
    )

    resolved = resolve_post_batch_decision(signals)

    assert resolved.action == "close_stage"
    assert resolved.is_stage_closed is True
    assert resolved.continue_execution is False
    assert resolved.requires_replanning is False
    assert resolved.requires_resequencing is False
    assert resolved.requires_manual_review is False


def test_resolve_post_batch_decision_requires_manual_review_when_flagged():
    signals = make_signals(
        decision="manual_review_required",
        manual_review_required=True,
        recommended_next_action="manual_review",
    )

    resolved = resolve_post_batch_decision(signals)

    assert resolved.action == "manual_review"
    assert resolved.requires_manual_review is True
    assert resolved.continue_execution is False
    assert resolved.requires_replanning is False
    assert resolved.requires_resequencing is False


def test_resolve_post_batch_decision_replans_when_remaining_plan_is_not_valid():
    signals = make_signals(
        remaining_plan_still_valid=False,
        recommended_next_action="replan_remaining_work",
        plan_change_scope="high_level_replan",
        replan_required=True,
        replan_level="high_level",
    )

    resolved = resolve_post_batch_decision(signals)

    assert resolved.action == "replan_remaining_work"
    assert resolved.requires_replanning is True
    assert resolved.requires_resequencing is False
    assert resolved.continue_execution is False
    assert resolved.reopened_finalization is True


def test_resolve_post_batch_decision_resequences_when_new_recovery_work_blocks():
    signals = make_signals(
        remaining_plan_still_valid=True,
        recommended_next_action="resequence_remaining_batches",
        plan_change_scope="local_resequencing",
        new_recovery_tasks_created=True,
        has_new_recovery_pending_tasks=True,
        new_recovery_pending_task_count=2,
        new_recovery_tasks_blocking=True,
        followup_atomic_tasks_required=True,
    )

    resolved = resolve_post_batch_decision(signals)

    assert resolved.action == "resequence_remaining_batches"
    assert resolved.requires_resequencing is True
    assert resolved.requires_replanning is False
    assert resolved.continue_execution is False
    assert resolved.reopened_finalization is True


def test_resolve_post_batch_decision_continues_when_preexisting_backlog_is_valid():
    signals = make_signals(
        remaining_plan_still_valid=True,
        recommended_next_action="continue_current_plan",
        has_pending_valid_tasks=True,
        has_preexisting_pending_valid_tasks=True,
        preexisting_pending_valid_task_count=3,
        has_new_recovery_pending_tasks=False,
        new_recovery_tasks_blocking=False,
    )

    resolved = resolve_post_batch_decision(signals)

    assert resolved.action == "continue_current_plan"
    assert resolved.continue_execution is True
    assert resolved.requires_replanning is False
    assert resolved.requires_resequencing is False
    assert resolved.requires_manual_review is False
    assert resolved.reopened_finalization is False


def test_resolve_post_batch_decision_defaults_to_continue_when_plan_is_still_valid():
    signals = make_signals(
        remaining_plan_still_valid=True,
        recommended_next_action="",
        has_pending_valid_tasks=False,
        has_preexisting_pending_valid_tasks=False,
        has_new_recovery_pending_tasks=False,
        new_recovery_tasks_blocking=None,
        manual_review_required=False,
    )

    resolved = resolve_post_batch_decision(signals)

    assert resolved.action == "continue_current_plan"
    assert resolved.continue_execution is True
    assert resolved.requires_manual_review is False
    assert resolved.requires_replanning is False
    assert resolved.requires_resequencing is False


def test_resolve_post_batch_decision_does_not_replan_for_non_blocking_new_recovery_tasks():
    signals = make_signals(
        remaining_plan_still_valid=True,
        recommended_next_action="continue_current_plan",
        new_recovery_tasks_created=True,
        has_new_recovery_pending_tasks=True,
        new_recovery_pending_task_count=1,
        new_recovery_tasks_blocking=False,
        has_preexisting_pending_valid_tasks=True,
        preexisting_pending_valid_task_count=2,
    )

    resolved = resolve_post_batch_decision(signals)

    assert resolved.requires_replanning is False
    assert resolved.action == "continue_current_plan"

def test_resolve_post_batch_decision_resequences_when_recovery_work_requires_precedence_without_structural_replan():
    signals = make_signals(
        remaining_plan_still_valid=True,
        recommended_next_action="",
        plan_change_scope="none",
        replan_required=False,
        new_recovery_tasks_created=True,
        has_new_recovery_pending_tasks=True,
        new_recovery_pending_task_count=1,
        new_recovery_tasks_blocking=True,
        followup_atomic_tasks_required=False,
        has_preexisting_pending_valid_tasks=True,
        preexisting_pending_valid_task_count=2,
    )

    resolved = resolve_post_batch_decision(signals)

    assert resolved.action == "resequence_remaining_batches"
    assert resolved.requires_resequencing is True
    assert resolved.requires_replanning is False
    assert resolved.continue_execution is False


def test_resolve_post_batch_decision_does_not_resequence_when_new_recovery_work_is_non_blocking_and_plan_is_valid():
    signals = make_signals(
        remaining_plan_still_valid=True,
        recommended_next_action="",
        new_recovery_tasks_created=True,
        has_new_recovery_pending_tasks=True,
        new_recovery_pending_task_count=1,
        new_recovery_tasks_blocking=False,
        has_preexisting_pending_valid_tasks=False,
        preexisting_pending_valid_task_count=0,
        followup_atomic_tasks_required=False,
        single_task_tail_risk=False,
    )

    resolved = resolve_post_batch_decision(signals)

    assert resolved.action == "continue_current_plan"
    assert resolved.continue_execution is True
    assert resolved.requires_replanning is False
    assert resolved.requires_resequencing is False