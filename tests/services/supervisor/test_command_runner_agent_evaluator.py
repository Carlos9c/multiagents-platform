"""
Tests for CommandRunnerAgentEvaluator and CommandRunnerAgentValidatorEvaluator.

Key difference: the command_runner executor evaluator receives command_trace_entries
in the tasks context (populated from execution_trace.jsonl).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.task import TASK_STATUS_FAILED
from app.services.supervisor.evaluators.command_runner_agent_evaluator import (
    CommandRunnerAgentEvaluator,
)
from app.services.supervisor.evaluators.command_runner_agent_validator_evaluator import (
    CommandRunnerAgentValidatorEvaluator,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "Command execution patterns are consistent and well-evidenced.",
    "issues": [],
    "suggestions": [],
}


def _one_task_context(project_id: int = 1) -> dict:
    return {
        "project_id": project_id,
        "agent_name": "command_runner_agent",
        "validator_name": "command_runner_agent_validator",
        "tasks": [
            {
                "task_id": 201,
                "task_title": "Run database migrations",
                "task_type": "implementation",
                "task_objective": "Execute Alembic migrations",
                "acceptance_criteria": "No migration errors",
                "task_status": TASK_STATUS_FAILED,
                "had_budget_exceeded": False,
                "runs": [
                    {
                        "run_id": 10,
                        "attempt_number": 1,
                        "run_status": "failed",
                        "work_summary": "Ran alembic upgrade head",
                        "changed_files": [],
                        "command_trace_entries": [
                            {
                                "agent": "command_runner_agent",
                                "call_type": "command_executed",
                                "commands": [
                                    {
                                        "command": "alembic upgrade head",
                                        "exit_code": 1,
                                        "success": False,
                                        "stdout_preview": "",
                                        "stderr_preview": "FAILED: migration conflict",
                                    }
                                ],
                            }
                        ],
                        "joint_validation_decision": "failed",
                        "validator_result": {
                            "decision": "failed",
                            "summary": "Migration command failed with exit code 1",
                        },
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# CommandRunnerAgentEvaluator — executor side
# ---------------------------------------------------------------------------


def test_executor_returns_healthy_when_no_tasks(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No CRA tasks")
    with patch(
        "app.services.supervisor.evaluators.command_runner_agent_evaluator.build_pair_evaluation_context",
        return_value={
            "project_id": proj.id,
            "agent_name": "command_runner_agent",
            "validator_name": "command_runner_agent_validator",
            "tasks": [],
        },
    ):
        result = CommandRunnerAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_executor_calls_llm_once(
    db_session: Session,
    make_project,
):
    proj = make_project(name="CRA executor LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_evaluator.resolve_system_prompt",
            return_value="You are an executor evaluator.",
        ),
    ):
        result = CommandRunnerAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_executor_prompt_includes_command_trace_data(
    db_session: Session,
    make_project,
):
    """Command trace entries must appear in the executor evaluator's user prompt."""
    proj = make_project(name="CRA executor command trace test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_evaluator.resolve_system_prompt",
            return_value="You are an executor evaluator.",
        ),
    ):
        CommandRunnerAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert "alembic upgrade head" in prompt
    assert "Run database migrations" in prompt


def test_executor_retries_on_invalid_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="CRA executor retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_evaluator.resolve_system_prompt",
            return_value="You are an executor evaluator.",
        ),
    ):
        result = CommandRunnerAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"


# ---------------------------------------------------------------------------
# CommandRunnerAgentValidatorEvaluator — validator side
# ---------------------------------------------------------------------------


def test_validator_returns_healthy_when_no_tasks(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No CRAV tasks")
    with patch(
        "app.services.supervisor.evaluators.command_runner_agent_validator_evaluator.build_pair_evaluation_context",
        return_value={
            "project_id": proj.id,
            "agent_name": "command_runner_agent",
            "validator_name": "command_runner_agent_validator",
            "tasks": [],
        },
    ):
        result = CommandRunnerAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_validator_calls_llm_once(
    db_session: Session,
    make_project,
):
    proj = make_project(name="CRAV validator LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        result = CommandRunnerAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_validator_prompt_includes_command_trace_entries(
    db_session: Session,
    make_project,
):
    """The validator evaluator receives command_trace_entries (grounding its findings)."""
    proj = make_project(name="CRAV validator prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        CommandRunnerAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert "alembic upgrade head" in prompt
    assert "Run database migrations" in prompt


def test_validator_retries_on_invalid_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="CRAV validator retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.command_runner_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        result = CommandRunnerAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"
