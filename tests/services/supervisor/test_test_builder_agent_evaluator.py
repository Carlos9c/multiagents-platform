"""
Tests for TestBuilderAgentEvaluator and TestBuilderAgentValidatorEvaluator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.task import TASK_STATUS_PARTIAL
from app.services.supervisor.evaluators.test_builder_agent_evaluator import (
    TestBuilderAgentEvaluator,
)
from app.services.supervisor.evaluators.test_builder_agent_validator_evaluator import (
    TestBuilderAgentValidatorEvaluator,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "Test generation patterns are consistent and acceptance_criteria-driven.",
    "issues": [],
    "suggestions": [],
}


def _one_task_context(project_id: int = 1) -> dict:
    return {
        "project_id": project_id,
        "agent_name": "test_builder_agent",
        "validator_name": "test_builder_agent_validator",
        "tasks": [
            {
                "task_id": 301,
                "task_title": "Write tests for auth module",
                "task_type": "testing",
                "task_objective": "Cover all acceptance criteria with tests",
                "acceptance_criteria": "Login endpoint returns 200 on valid credentials",
                "task_status": TASK_STATUS_PARTIAL,
                "had_budget_exceeded": False,
                "runs": [
                    {
                        "run_id": 20,
                        "attempt_number": 1,
                        "run_status": "succeeded",
                        "work_summary": "Created tests/auth/test_login.py",
                        "changed_files": ["tests/auth/test_login.py"],
                        "command_trace_entries": [],
                        "joint_validation_decision": "partial",
                        "validator_result": {
                            "decision": "partial",
                            "summary": "Missing edge case tests",
                            "partial_annotations": [
                                "test_login_invalid_credentials is a placeholder"
                            ],
                        },
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# TestBuilderAgentEvaluator — executor side
# ---------------------------------------------------------------------------


def test_executor_returns_healthy_when_no_tasks(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No TBA tasks")
    with patch(
        "app.services.supervisor.evaluators.test_builder_agent_evaluator.build_pair_evaluation_context",
        return_value={
            "project_id": proj.id,
            "agent_name": "test_builder_agent",
            "validator_name": "test_builder_agent_validator",
            "tasks": [],
        },
    ):
        result = TestBuilderAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_executor_calls_llm_once(
    db_session: Session,
    make_project,
):
    proj = make_project(name="TBA executor LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_evaluator.resolve_system_prompt",
            return_value="You are a test builder evaluator.",
        ),
    ):
        result = TestBuilderAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_executor_prompt_includes_acceptance_criteria(
    db_session: Session,
    make_project,
):
    """Acceptance criteria must appear in the prompt for scope coverage assessment."""
    proj = make_project(name="TBA executor criteria test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_evaluator.resolve_system_prompt",
            return_value="You are a test builder evaluator.",
        ),
    ):
        TestBuilderAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert "Write tests for auth module" in prompt
    assert "Login endpoint returns 200" in prompt


def test_executor_retries_on_invalid_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="TBA executor retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_evaluator.resolve_system_prompt",
            return_value="You are a test builder evaluator.",
        ),
    ):
        result = TestBuilderAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"


# ---------------------------------------------------------------------------
# TestBuilderAgentValidatorEvaluator — validator side
# ---------------------------------------------------------------------------


def test_validator_returns_healthy_when_no_tasks(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No TBAV tasks")
    with patch(
        "app.services.supervisor.evaluators.test_builder_agent_validator_evaluator.build_pair_evaluation_context",
        return_value={
            "project_id": proj.id,
            "agent_name": "test_builder_agent",
            "validator_name": "test_builder_agent_validator",
            "tasks": [],
        },
    ):
        result = TestBuilderAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_validator_calls_llm_once(
    db_session: Session,
    make_project,
):
    proj = make_project(name="TBAV validator LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        result = TestBuilderAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_validator_prompt_includes_partial_annotations(
    db_session: Session,
    make_project,
):
    """partial_annotations from validator_result must appear in the validator prompt."""
    proj = make_project(name="TBAV validator annotations test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        TestBuilderAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert "test_login_invalid_credentials is a placeholder" in prompt
    assert "Write tests for auth module" in prompt


def test_validator_retries_on_invalid_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="TBAV validator retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.test_builder_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        result = TestBuilderAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"
