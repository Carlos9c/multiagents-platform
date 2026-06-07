from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.supervisor.evaluators.image_generation_agent_evaluator import (
    ImageGenerationAgentEvaluator,
)
from app.services.supervisor.evaluators.image_generation_agent_validator_evaluator import (
    ImageGenerationAgentValidatorEvaluator,
)


def _make_db() -> MagicMock:
    return MagicMock()


def _healthy_output() -> dict:
    return {
        "verdict": "healthy",
        "findings": "Prompt engineering is consistent and specific across all tasks.",
        "issues": [],
        "suggestions": [],
    }


# ── ImageGenerationAgentEvaluator ────────────────────────────────────────────


def test_executor_evaluator_returns_not_supervised_when_no_data():
    with patch(
        "app.services.supervisor.evaluators.image_generation_agent_evaluator.build_pair_evaluation_context",
        return_value={"tasks": []},
    ):
        evaluator = ImageGenerationAgentEvaluator()
        result = evaluator.evaluate(db=_make_db(), project_id=1)

    assert result.result is None


def test_executor_evaluator_calls_llm_with_data():
    llm = MagicMock()
    llm.generate_structured.return_value = _healthy_output()

    with (
        patch(
            "app.services.supervisor.evaluators.image_generation_agent_evaluator.build_pair_evaluation_context",
            return_value={
                "tasks": [
                    {
                        "task_id": 1,
                        "task_title": "Generate icon",
                        "runs": [{"run_id": 10, "decision": "completed"}],
                    }
                ]
            },
        ),
        patch(
            "app.services.supervisor.evaluators.image_generation_agent_evaluator.get_llm_provider",
            return_value=llm,
        ),
        patch(
            "app.services.supervisor.evaluators.image_generation_agent_evaluator.resolve_system_prompt",
            return_value="system prompt",
        ),
        patch(
            "app.services.supervisor.evaluators.image_generation_agent_evaluator.get_system_versions_for_runs",
            return_value=[],
        ),
    ):
        evaluator = ImageGenerationAgentEvaluator()
        result = evaluator.evaluate(db=_make_db(), project_id=1)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    assert llm.generate_structured.called


def test_executor_evaluator_agent_name():
    assert ImageGenerationAgentEvaluator.AGENT_NAME == "image_generation_agent"


# ── ImageGenerationAgentValidatorEvaluator ───────────────────────────────────


def test_validator_evaluator_returns_not_supervised_when_no_data():
    with patch(
        "app.services.supervisor.evaluators.image_generation_agent_validator_evaluator.build_pair_evaluation_context",
        return_value={"tasks": []},
    ):
        evaluator = ImageGenerationAgentValidatorEvaluator()
        result = evaluator.evaluate(db=_make_db(), project_id=1)

    assert result.result is None


def test_validator_evaluator_calls_llm_with_data():
    llm = MagicMock()
    llm.generate_structured.return_value = _healthy_output()

    with (
        patch(
            "app.services.supervisor.evaluators.image_generation_agent_validator_evaluator.build_pair_evaluation_context",
            return_value={
                "tasks": [
                    {
                        "task_id": 1,
                        "task_title": "Validate icon",
                        "runs": [{"run_id": 11, "decision": "completed"}],
                    }
                ]
            },
        ),
        patch(
            "app.services.supervisor.evaluators.image_generation_agent_validator_evaluator.get_llm_provider",
            return_value=llm,
        ),
        patch(
            "app.services.supervisor.evaluators.image_generation_agent_validator_evaluator.resolve_system_prompt",
            return_value="validator system prompt",
        ),
        patch(
            "app.services.supervisor.evaluators.image_generation_agent_validator_evaluator.get_system_versions_for_runs",
            return_value=[],
        ),
    ):
        evaluator = ImageGenerationAgentValidatorEvaluator()
        result = evaluator.evaluate(db=_make_db(), project_id=1)

    assert result.result is not None
    assert result.result.verdict == "healthy"


def test_validator_evaluator_agent_name():
    assert ImageGenerationAgentValidatorEvaluator.AGENT_NAME == "image_generation_agent_validator"
