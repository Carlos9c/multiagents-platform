"""
Tests for RequirementsEvaluatorEvaluator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators.requirements_evaluator_evaluator import (
    RequirementsEvaluatorEvaluator,
)

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "The requirements_evaluator demonstrated well-calibrated question progression.",
    "issues": [],
    "suggestions": [],
}

_MODULE = "app.services.supervisor.evaluators.requirements_evaluator_evaluator"


def _one_evaluation_context() -> list[dict]:
    return [
        {
            "artifact_id": 11,
            "call_index": 0,
            "status": "needs_more",
            "next_question": "What tech stack do you prefer?",
            "reasoning": "Technology constraints not yet specified.",
            "draft_snapshot": "User wants to build a web application.",
            "conversation_turn_count": 2,
        },
        {
            "artifact_id": 12,
            "call_index": 1,
            "status": "sufficient",
            "next_question": None,
            "reasoning": "All required information gathered.",
            "draft_snapshot": "FastAPI web application with PostgreSQL backend.",
            "conversation_turn_count": 4,
        },
    ]


def test_returns_healthy_when_no_evaluations(db_session: Session, make_project):
    proj = make_project(name="No req evaluations")
    with patch(
        f"{_MODULE}.load_requirements_evaluation_artifacts",
        return_value=[],
    ):
        result = RequirementsEvaluatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_calls_llm_once_when_evaluations_present(db_session: Session, make_project):
    proj = make_project(name="Req evaluator LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            f"{_MODULE}.load_requirements_evaluation_artifacts",
            return_value=_one_evaluation_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a requirements evaluator supervisor.",
        ),
    ):
        result = RequirementsEvaluatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_user_prompt_includes_evaluation_data(db_session: Session, make_project):
    proj = make_project(name="Req evaluator prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_requirements_evaluation_artifacts",
            return_value=_one_evaluation_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a requirements evaluator supervisor.",
        ),
    ):
        RequirementsEvaluatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert str(proj.id) in prompt
    assert "needs_more" in prompt


def test_retries_on_invalid_llm_output(db_session: Session, make_project):
    proj = make_project(name="Req evaluator retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            f"{_MODULE}.load_requirements_evaluation_artifacts",
            return_value=_one_evaluation_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a requirements evaluator supervisor.",
        ),
    ):
        result = RequirementsEvaluatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"


def test_system_prompt_passed_to_user_prompt(db_session: Session, make_project):
    proj = make_project(name="Req evaluator system prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()
    sentinel = "UNIQUE_REQUIREMENTS_EVALUATOR_SYSTEM_PROMPT"

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_requirements_evaluation_artifacts",
            return_value=_one_evaluation_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        patch(f"{_MODULE}.resolve_system_prompt", return_value=sentinel),
    ):
        RequirementsEvaluatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert sentinel in captured[0]


def test_agent_name_is_requirements_evaluator():
    assert RequirementsEvaluatorEvaluator.AGENT_NAME == "requirements_evaluator"
