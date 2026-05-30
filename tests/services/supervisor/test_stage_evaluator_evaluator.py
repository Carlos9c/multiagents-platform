"""
Tests for StageEvaluatorEvaluator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators.stage_evaluator_evaluator import (
    StageEvaluatorEvaluator,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "The stage_evaluator demonstrated well-calibrated checkpoint decisions.",
    "issues": [],
    "suggestions": [],
}

_MODULE = "app.services.supervisor.evaluators.stage_evaluator_evaluator"


def _one_evaluation_context() -> list[dict]:
    return [
        {
            "artifact_id": 42,
            "decision": "stage_incomplete",
            "decision_summary": "Three tasks failed validation; recovery required.",
            "stage_goals_satisfied": False,
            "project_stage_closed": False,
            "recovery_strategy": "reatomize",
            "evaluated_batches": [
                {
                    "batch_id": "batch-1",
                    "outcome": "partial",
                    "summary": "Two tasks completed, one failed.",
                    "failed_task_ids": [7],
                    "partial_task_ids": [],
                    "completed_task_ids": [5, 6],
                }
            ],
            "key_risks": ["Auth module incomplete"],
            "notes": [],
        }
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_returns_healthy_when_no_evaluations(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No stage evaluations")
    with patch(
        f"{_MODULE}.load_evaluation_decision_artifacts",
        return_value=[],
    ):
        result = StageEvaluatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_calls_llm_once_when_evaluations_present(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Stage evaluator LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            f"{_MODULE}.load_evaluation_decision_artifacts",
            return_value=_one_evaluation_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a stage evaluator supervisor.",
        ),
    ):
        result = StageEvaluatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_user_prompt_includes_evaluation_data(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Stage evaluator prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_evaluation_decision_artifacts",
            return_value=_one_evaluation_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a stage evaluator supervisor.",
        ),
    ):
        StageEvaluatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert str(proj.id) in prompt
    assert "stage_incomplete" in prompt


def test_retries_on_invalid_llm_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Stage evaluator retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            f"{_MODULE}.load_evaluation_decision_artifacts",
            return_value=_one_evaluation_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a stage evaluator supervisor.",
        ),
    ):
        result = StageEvaluatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"


def test_system_prompt_passed_to_user_prompt(
    db_session: Session,
    make_project,
):
    """resolve_system_prompt output appears in the LLM user prompt."""
    proj = make_project(name="Stage evaluator system prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()
    sentinel = "UNIQUE_STAGE_EVALUATOR_SYSTEM_PROMPT_CONTENT"

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_evaluation_decision_artifacts",
            return_value=_one_evaluation_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value=sentinel,
        ),
    ):
        StageEvaluatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert sentinel in captured[0]


def test_agent_name_is_stage_evaluator():
    assert StageEvaluatorEvaluator.AGENT_NAME == "stage_evaluator"
