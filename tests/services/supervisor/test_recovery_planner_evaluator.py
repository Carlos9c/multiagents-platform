"""
Tests for RecoveryPlannerEvaluator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators.recovery_planner_evaluator import (
    RecoveryPlannerEvaluator,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "The recovery_planner demonstrated appropriate action calibration.",
    "issues": [],
    "suggestions": [],
}

_MODULE = "app.services.supervisor.evaluators.recovery_planner_evaluator"


def _one_decision_context() -> list[dict]:
    return [
        {
            "artifact_id": 77,
            "source_task_id": 10,
            "source_run_id": 100,
            "action": "reatomize",
            "confidence": "medium",
            "reason": "Task scope was too broad to complete atomically.",
            "covered_gap_summary": "Split into smaller focused tasks.",
            "requires_manual_review": False,
            "still_blocks_progress": True,
            "created_tasks": [
                {
                    "title": "Implement auth module",
                    "description": "Create the authentication module with JWT support.",
                    "task_type": "implementation",
                }
            ],
            "source_run_context": {
                "run_id": 100,
                "failure_type": "validation_failed",
                "failure_code": None,
                "work_summary": "Partially implemented auth; JWT missing.",
                "blockers_found": ["JWT library not installed"],
                "validation_notes": "Validator found incomplete implementation.",
            },
            "source_task_context": {
                "task_id": 10,
                "task_type": "implementation",
                "objective": "Build full auth system",
                "acceptance_criteria": "JWT login and token refresh working",
                "status": "failed",
            },
        }
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_returns_healthy_when_no_decisions(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No recovery decisions")
    with patch(
        f"{_MODULE}.load_recovery_decision_artifacts",
        return_value=[],
    ):
        result = RecoveryPlannerEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_calls_llm_once_when_decisions_present(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Recovery planner LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            f"{_MODULE}.load_recovery_decision_artifacts",
            return_value=_one_decision_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a recovery planner supervisor.",
        ),
    ):
        result = RecoveryPlannerEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_user_prompt_includes_decision_data(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Recovery planner prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_recovery_decision_artifacts",
            return_value=_one_decision_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a recovery planner supervisor.",
        ),
    ):
        RecoveryPlannerEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert str(proj.id) in prompt
    assert "reatomize" in prompt


def test_retries_on_invalid_llm_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Recovery planner retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            f"{_MODULE}.load_recovery_decision_artifacts",
            return_value=_one_decision_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a recovery planner supervisor.",
        ),
    ):
        result = RecoveryPlannerEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"


def test_system_prompt_passed_to_user_prompt(
    db_session: Session,
    make_project,
):
    """resolve_system_prompt output appears in the LLM user prompt."""
    proj = make_project(name="Recovery planner system prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()
    sentinel = "UNIQUE_RECOVERY_PLANNER_SYSTEM_PROMPT_CONTENT"

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_recovery_decision_artifacts",
            return_value=_one_decision_context(),
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
        RecoveryPlannerEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert sentinel in captured[0]


def test_agent_name_is_recovery_planner():
    assert RecoveryPlannerEvaluator.AGENT_NAME == "recovery_planner"
