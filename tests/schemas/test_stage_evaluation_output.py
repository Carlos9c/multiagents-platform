import pytest
from pydantic import ValidationError

from app.schemas.evaluation import EvaluationReplanInstruction, StageEvaluationOutput


def test_stage_evaluation_output_rejects_contradictory_replan_signals():
    with pytest.raises(ValidationError):
        StageEvaluationOutput(
            decision="stage_incomplete",
            decision_summary="Contradictory signals.",
            stage_goals_satisfied=False,
            project_stage_closed=False,
            recovery_strategy="none",
            recovery_reason=None,
            replan=EvaluationReplanInstruction(
                required=True,
                level="high_level",
                reason="Contradictory replan.",
                target_task_ids=[],
            ),
            followup_atomic_tasks_required=False,
            followup_atomic_tasks_reason=None,
            manual_review_required=False,
            manual_review_reason=None,
            recommended_next_action="replan_remaining_work",
            recommended_next_action_reason="Contradiction.",
            decision_signals=["remaining_plan_still_valid", "force_replan"],
            plan_change_scope="high_level_replan",
            remaining_plan_still_valid=True,  # 🔥 contradicción real
            new_recovery_tasks_blocking=False,
            single_task_tail_risk=False,
            evaluated_batches=[],
            key_risks=[],
            notes=[],
        )