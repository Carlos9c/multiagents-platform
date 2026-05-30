"""
Tests for CodeChangeAgentEvaluator and CodeChangeAgentValidatorEvaluator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.task import TASK_STATUS_PARTIAL
from app.services.supervisor.evaluators.code_change_agent_evaluator import (
    CodeChangeAgentEvaluator,
)
from app.services.supervisor.evaluators.code_change_agent_validator_evaluator import (
    CodeChangeAgentValidatorEvaluator,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "The agent demonstrated consistent, well-scoped behavior.",
    "issues": [],
    "suggestions": [],
}


def _patch_context_no_tasks():
    return patch(
        "app.services.supervisor.evaluators.code_change_agent_evaluator.build_pair_evaluation_context",
        return_value={
            "project_id": 1,
            "agent_name": "code_change_agent",
            "validator_name": "code_change_agent_validator",
            "tasks": [],
        },
    )


def _patch_context_no_tasks_validator():
    return patch(
        "app.services.supervisor.evaluators.code_change_agent_validator_evaluator.build_pair_evaluation_context",
        return_value={
            "project_id": 1,
            "agent_name": "code_change_agent",
            "validator_name": "code_change_agent_validator",
            "tasks": [],
        },
    )


def _one_task_context(project_id: int = 1, task_id: int = 101) -> dict:
    return {
        "project_id": project_id,
        "agent_name": "code_change_agent",
        "validator_name": "code_change_agent_validator",
        "tasks": [
            {
                "task_id": task_id,
                "task_title": "Implement login endpoint",
                "task_type": "implementation",
                "task_objective": "Add POST /login route",
                "acceptance_criteria": "Returns 200 on valid credentials",
                "task_status": TASK_STATUS_PARTIAL,
                "had_budget_exceeded": False,
                "runs": [
                    {
                        "run_id": 1,
                        "attempt_number": 1,
                        "run_status": "succeeded",
                        "work_summary": "Created auth module",
                        "changed_files": ["app/auth/routes.py"],
                        "joint_validation_decision": "partial",
                        "validator_result": {
                            "decision": "partial",
                            "summary": "Missing input validation",
                        },
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# CodeChangeAgentEvaluator — executor side
# ---------------------------------------------------------------------------


def test_executor_returns_healthy_when_no_tasks(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No CCA tasks")
    with _patch_context_no_tasks():
        result = CodeChangeAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_executor_calls_llm_once(
    db_session: Session,
    make_project,
):
    proj = make_project(name="CCA executor LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            "app.services.supervisor.evaluators.code_change_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_evaluator.resolve_system_prompt",
            return_value="You are an executor evaluator.",
        ),
    ):
        result = CodeChangeAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_executor_prompt_includes_task_data(
    db_session: Session,
    make_project,
):
    proj = make_project(name="CCA executor prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.code_change_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_evaluator.resolve_system_prompt",
            return_value="You are an executor evaluator.",
        ),
    ):
        CodeChangeAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert "Implement login endpoint" in prompt
    assert str(proj.id) in prompt


def test_executor_retries_on_invalid_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="CCA executor retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.code_change_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_evaluator.resolve_system_prompt",
            return_value="You are an executor evaluator.",
        ),
    ):
        result = CodeChangeAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"


# ---------------------------------------------------------------------------
# CodeChangeAgentValidatorEvaluator — validator side
# ---------------------------------------------------------------------------


def test_validator_returns_healthy_when_no_tasks(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No CCAV tasks")
    with _patch_context_no_tasks_validator():
        result = CodeChangeAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_validator_calls_llm_once(
    db_session: Session,
    make_project,
):
    proj = make_project(name="CCAV validator LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            "app.services.supervisor.evaluators.code_change_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        result = CodeChangeAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_validator_prompt_includes_task_data(
    db_session: Session,
    make_project,
):
    proj = make_project(name="CCAV validator prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.code_change_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        CodeChangeAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert "Implement login endpoint" in prompt
    assert str(proj.id) in prompt


def test_validator_retries_on_invalid_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="CCAV validator retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.code_change_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.code_change_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        result = CodeChangeAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"
