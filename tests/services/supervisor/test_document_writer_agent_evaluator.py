"""
Tests for DocumentWriterAgentEvaluator and DocumentWriterAgentValidatorEvaluator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.task import TASK_STATUS_FAILED
from app.services.supervisor.evaluators.document_writer_agent_evaluator import (
    DocumentWriterAgentEvaluator,
)
from app.services.supervisor.evaluators.document_writer_agent_validator_evaluator import (
    DocumentWriterAgentValidatorEvaluator,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "Documentation generation patterns are consistent and format-appropriate.",
    "issues": [],
    "suggestions": [],
}


def _one_task_context(project_id: int = 1) -> dict:
    return {
        "project_id": project_id,
        "agent_name": "document_writer_agent",
        "validator_name": "document_writer_agent_validator",
        "tasks": [
            {
                "task_id": 401,
                "task_title": "Write API reference documentation",
                "task_type": "documentation",
                "task_objective": "Produce OpenAPI spec for /users endpoints",
                "acceptance_criteria": "Must include all request/response schemas",
                "task_status": TASK_STATUS_FAILED,
                "had_budget_exceeded": False,
                "runs": [
                    {
                        "run_id": 30,
                        "attempt_number": 1,
                        "run_status": "failed",
                        "work_summary": "Generated partial OpenAPI spec",
                        "changed_files": ["docs/api/openapi.yaml"],
                        "command_trace_entries": [],
                        "joint_validation_decision": "failed",
                        "validator_result": {
                            "decision": "failed",
                            "summary": "Response schemas are missing",
                            "partial_annotations": ["GET /users response schema not defined"],
                        },
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# DocumentWriterAgentEvaluator — executor side
# ---------------------------------------------------------------------------


def test_executor_returns_healthy_when_no_tasks(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No DWA tasks")
    with patch(
        "app.services.supervisor.evaluators.document_writer_agent_evaluator.build_pair_evaluation_context",
        return_value={
            "project_id": proj.id,
            "agent_name": "document_writer_agent",
            "validator_name": "document_writer_agent_validator",
            "tasks": [],
        },
    ):
        result = DocumentWriterAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_executor_calls_llm_once(
    db_session: Session,
    make_project,
):
    proj = make_project(name="DWA executor LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_evaluator.resolve_system_prompt",
            return_value="You are a document writer evaluator.",
        ),
    ):
        result = DocumentWriterAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_executor_prompt_includes_task_data(
    db_session: Session,
    make_project,
):
    proj = make_project(name="DWA executor prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_evaluator.resolve_system_prompt",
            return_value="You are a document writer evaluator.",
        ),
    ):
        DocumentWriterAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert "Write API reference documentation" in prompt
    assert str(proj.id) in prompt


def test_executor_retries_on_invalid_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="DWA executor retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_evaluator.resolve_system_prompt",
            return_value="You are a document writer evaluator.",
        ),
    ):
        result = DocumentWriterAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"


# ---------------------------------------------------------------------------
# DocumentWriterAgentValidatorEvaluator — validator side
# ---------------------------------------------------------------------------


def test_validator_returns_healthy_when_no_tasks(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No DWAV tasks")
    with patch(
        "app.services.supervisor.evaluators.document_writer_agent_validator_evaluator.build_pair_evaluation_context",
        return_value={
            "project_id": proj.id,
            "agent_name": "document_writer_agent",
            "validator_name": "document_writer_agent_validator",
            "tasks": [],
        },
    ):
        result = DocumentWriterAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_validator_calls_llm_once(
    db_session: Session,
    make_project,
):
    proj = make_project(name="DWAV validator LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        result = DocumentWriterAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_validator_prompt_includes_partial_annotations(
    db_session: Session,
    make_project,
):
    """Partial annotations from the validator result must appear in the prompt."""
    proj = make_project(name="DWAV validator annotations test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        DocumentWriterAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert "GET /users response schema not defined" in prompt
    assert "Write API reference documentation" in prompt


def test_validator_retries_on_invalid_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="DWAV validator retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_validator_evaluator.build_pair_evaluation_context",
            return_value=_one_task_context(proj.id),
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_validator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.document_writer_agent_validator_evaluator.resolve_system_prompt",
            return_value="You are a validator evaluator.",
        ),
    ):
        result = DocumentWriterAgentValidatorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"
