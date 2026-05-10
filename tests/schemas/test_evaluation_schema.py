import pytest
from pydantic import ValidationError

from app.schemas.evaluation import (
    EvaluatedBatchSummary,
    EvaluationReplanInstruction,
    StageEvaluationOutput,
)


def _base_output(**overrides) -> dict:
    data = {
        "decision": "stage_incomplete",
        "decision_summary": "The stage is incomplete but the remaining work is still represented correctly.",
        "stage_goals_satisfied": False,
        "project_stage_closed": False,
        "recovery_strategy": "none",
        "recovery_reason": None,
        "replan": EvaluationReplanInstruction(
            required=False,
            level=None,
            reason=None,
            target_task_ids=[],
        ),
        "followup_atomic_tasks_required": False,
        "followup_atomic_tasks_reason": None,
        "manual_review_required": False,
        "manual_review_reason": None,
        "recommended_next_action": "continue_current_plan",
        "recommended_next_action_reason": "The current remaining backlog already represents the correct next work.",
        "decision_signals": ["remaining_plan_still_valid"],
        "plan_change_scope": "none",
        "remaining_plan_still_valid": True,
        "new_recovery_tasks_blocking": None,
        "single_task_tail_risk": False,
        "evaluated_batches": [
            EvaluatedBatchSummary(
                batch_id="batch_1",
                outcome="successful",
                summary="The batch completed successfully and produced coherent evidence.",
                key_findings=["The batch outputs are aligned with the expected scope."],
                failed_task_ids=[],
                partial_task_ids=[],
                completed_task_ids=[1],
            )
        ],
        "key_risks": ["Later work is still pending."],
        "notes": ["Continue with the next planned batch."],
    }
    data.update(overrides)
    return data


def test_continue_current_plan_is_valid_with_none_scope_and_valid_remaining_plan():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            recommended_next_action="continue_current_plan",
            recommended_next_action_reason="The remaining work is already represented correctly.",
            plan_change_scope="none",
            remaining_plan_still_valid=True,
            followup_atomic_tasks_required=False,
            manual_review_required=False,
            replan=EvaluationReplanInstruction(required=False),
        )
    )

    assert output.recommended_next_action == "continue_current_plan"
    assert output.plan_change_scope == "none"
    assert output.remaining_plan_still_valid is True


def test_continue_current_plan_with_followup_tasks_derives_resequence():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            followup_atomic_tasks_required=True,
            followup_atomic_tasks_reason="A new follow-up task is required.",
            recommended_next_action="continue_current_plan",
        )
    )
    assert output.recommended_next_action == "resequence_remaining_batches"
    assert output.plan_change_scope == "local_resequencing"
    assert output.remaining_plan_still_valid is True


def test_continue_current_plan_with_replan_derives_resequence():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            replan=EvaluationReplanInstruction(
                required=True,
                level="atomic",
                reason="Atomic replanning was requested.",
                target_task_ids=[1],
            ),
            recommended_next_action="continue_current_plan",
        )
    )
    assert output.recommended_next_action == "resequence_remaining_batches"
    assert output.plan_change_scope == "local_resequencing"


def test_continue_current_plan_with_manual_review_derives_manual_review():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            manual_review_required=True,
            manual_review_reason="A human should inspect the result.",
            recommended_next_action="continue_current_plan",
        )
    )
    assert output.recommended_next_action == "manual_review"


def test_resequence_remaining_batches_is_valid_with_local_resequencing_scope():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            recovery_strategy="insert_followup_atomic_tasks",
            recovery_reason="A local follow-up task was created and should be regrouped.",
            followup_atomic_tasks_required=True,
            followup_atomic_tasks_reason="A local follow-up task should be executed in a later batch.",
            recommended_next_action="resequence_remaining_batches",
            recommended_next_action_reason="Regrouping avoids an awkward single-task validation loop.",
            decision_signals=[
                "remaining_plan_still_valid",
                "followup_tasks_created",
                "single_task_tail_risk",
            ],
            plan_change_scope="local_resequencing",
            remaining_plan_still_valid=True,
            new_recovery_tasks_blocking=False,
            single_task_tail_risk=True,
        )
    )

    assert output.recommended_next_action == "resequence_remaining_batches"
    assert output.plan_change_scope == "local_resequencing"
    assert output.remaining_plan_still_valid is True


def test_resequence_remaining_batches_is_valid_with_remaining_plan_rebuild_scope():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            recovery_strategy="reatomize_failed_tasks",
            recovery_reason="Local recovery changes how the remaining batches should be regrouped.",
            recommended_next_action="resequence_remaining_batches",
            recommended_next_action_reason="The remaining work is still valid but should be rebuilt into better batches.",
            decision_signals=[
                "remaining_plan_still_valid",
                "remaining_batches_need_regrouping",
            ],
            plan_change_scope="remaining_plan_rebuild",
            remaining_plan_still_valid=True,
            replan=EvaluationReplanInstruction(
                required=True,
                level="atomic",
                reason="Atomic-level resequencing is needed.",
                target_task_ids=[2, 3],
            ),
        )
    )

    assert output.recommended_next_action == "resequence_remaining_batches"
    assert output.plan_change_scope == "remaining_plan_rebuild"


def test_high_level_replan_overrides_resequence_action():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            recovery_strategy="replan_from_high_level",
            recovery_reason="The high-level plan is no longer adequate.",
            recommended_next_action="resequence_remaining_batches",
            recommended_next_action_reason="Contradictory — high_level replan takes priority.",
            plan_change_scope="local_resequencing",
            remaining_plan_still_valid=False,
            replan=EvaluationReplanInstruction(
                required=True,
                level="high_level",
                reason="High-level replanning is required.",
                target_task_ids=[10],
            ),
        )
    )
    assert output.recommended_next_action == "replan_remaining_work"
    assert output.plan_change_scope == "high_level_replan"
    assert output.remaining_plan_still_valid is False


def test_resequence_remaining_batches_auto_fixes_none_scope():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            recovery_strategy="insert_followup_atomic_tasks",
            recovery_reason="A local follow-up task was created.",
            followup_atomic_tasks_required=True,
            followup_atomic_tasks_reason="A local follow-up task exists.",
            recommended_next_action="resequence_remaining_batches",
            recommended_next_action_reason="The work should be regrouped.",
            plan_change_scope="none",
            remaining_plan_still_valid=True,
        )
    )
    assert output.recommended_next_action == "resequence_remaining_batches"
    assert output.plan_change_scope == "local_resequencing"


def test_replan_remaining_work_is_valid_only_with_high_level_replan_scope():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            recovery_strategy="replan_from_high_level",
            recovery_reason="The remaining work is no longer represented correctly.",
            recommended_next_action="replan_remaining_work",
            recommended_next_action_reason="A structural change invalidates the remaining plan.",
            decision_signals=[
                "structural_gap_detected",
                "high_level_plan_invalid",
            ],
            plan_change_scope="high_level_replan",
            remaining_plan_still_valid=False,
            replan=EvaluationReplanInstruction(
                required=True,
                level="high_level",
                reason="The remaining work must be replanned from the high-level stage layer.",
                target_task_ids=[20, 21],
            ),
        )
    )

    assert output.recommended_next_action == "replan_remaining_work"
    assert output.plan_change_scope == "high_level_replan"
    assert output.remaining_plan_still_valid is False


def test_replan_remaining_work_auto_fixes_remaining_plan_still_valid():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            recovery_strategy="replan_from_high_level",
            recovery_reason="The remaining work is no longer represented correctly.",
            recommended_next_action="replan_remaining_work",
            recommended_next_action_reason="A structural change invalidates the remaining plan.",
            plan_change_scope="high_level_replan",
            remaining_plan_still_valid=True,
            replan=EvaluationReplanInstruction(
                required=True,
                level="high_level",
                reason="High-level replanning is required.",
                target_task_ids=[20],
            ),
        )
    )
    assert output.remaining_plan_still_valid is False


def test_replan_remaining_work_auto_fixes_plan_change_scope():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            recovery_strategy="replan_from_high_level",
            recovery_reason="The remaining work is structurally invalid.",
            recommended_next_action="replan_remaining_work",
            recommended_next_action_reason="A structural change invalidates the remaining plan.",
            plan_change_scope="remaining_plan_rebuild",
            remaining_plan_still_valid=False,
            replan=EvaluationReplanInstruction(
                required=True,
                level="high_level",
                reason="High-level replanning is required.",
                target_task_ids=[30],
            ),
        )
    )
    assert output.plan_change_scope == "high_level_replan"


def test_stage_completed_allows_only_close_stage_or_none():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            decision="stage_completed",
            decision_summary="The stage goals are fully satisfied and the stage can be closed safely.",
            stage_goals_satisfied=True,
            project_stage_closed=True,
            recovery_strategy="none",
            recommended_next_action="close_stage",
            recommended_next_action_reason="The stage goals are satisfied and no additional work is needed.",
            decision_signals=["stage_goals_satisfied"],
            plan_change_scope="none",
            remaining_plan_still_valid=True,
            followup_atomic_tasks_required=False,
            manual_review_required=False,
            replan=EvaluationReplanInstruction(required=False),
            key_risks=[],
            notes=["Close the stage."],
        )
    )

    assert output.decision == "stage_completed"
    assert output.recommended_next_action == "close_stage"


def test_stage_completed_with_resequence_derives_close_stage():
    output = StageEvaluationOutput.model_validate(
        _base_output(
            decision="stage_completed",
            decision_summary="The stage goals are fully satisfied and the stage can be closed safely.",
            stage_goals_satisfied=True,
            project_stage_closed=True,
            recommended_next_action="resequence_remaining_batches",
            recommended_next_action_reason="Contradictory — stage_completed overrides.",
            recovery_strategy="none",
            plan_change_scope="local_resequencing",
            remaining_plan_still_valid=True,
        )
    )
    assert output.recommended_next_action == "close_stage"
    assert output.plan_change_scope == "none"


def test_stage_completed_rejects_followup_tasks():
    with pytest.raises(ValidationError, match="stage_completed"):
        StageEvaluationOutput.model_validate(
            _base_output(
                decision="stage_completed",
                decision_summary="The stage goals are fully satisfied and the stage can be closed safely.",
                stage_goals_satisfied=True,
                project_stage_closed=True,
                recommended_next_action="close_stage",
                recommended_next_action_reason="The stage is done.",
                followup_atomic_tasks_required=True,
                followup_atomic_tasks_reason="This should fail because a closed stage cannot require follow-up tasks.",
                plan_change_scope="none",
                remaining_plan_still_valid=True,
            )
        )
