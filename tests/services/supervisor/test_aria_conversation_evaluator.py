"""
Tests for AriaConversationEvaluator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators.aria_conversation_evaluator import (
    AriaConversationEvaluator,
)

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "Aria demonstrated coherent, helpful conversational behavior.",
    "issues": [],
    "suggestions": [],
}

_MODULE = "app.services.supervisor.evaluators.aria_conversation_evaluator"


def _one_conversation_context() -> dict:
    return {
        "conversation_id": 1,
        "final_phase": "completed",
        "review_episode_count": 1,
        "requirements_draft_preview": "FastAPI web application with PostgreSQL backend.",
        "message_count": 5,
        "messages": [
            {"role": "assistant", "content": "¡Hola! Cuéntame qué proyecto quieres construir."},
            {"role": "user", "content": "Quiero una app web con FastAPI."},
            {"role": "assistant", "content": "¿Qué base de datos necesitas?"},
            {"role": "user", "content": "PostgreSQL."},
            {
                "role": "assistant",
                "content": "Perfecto, tengo suficiente información para empezar.",
            },
        ],
        "project_queries": [
            {
                "artifact_id": 99,
                "query_index": 0,
                "conversation_phase": "paused",
                "user_question": "¿Qué se ha completado?",
                "aria_response": "Hemos completado 3 tareas.",
                "task_counts": {"completed": 3, "pending": 2, "failed": 0, "running": 0},
            }
        ],
    }


def _empty_conversation_context() -> dict:
    return {
        "conversation_id": 1,
        "final_phase": "gathering_requirements",
        "review_episode_count": 0,
        "requirements_draft_preview": None,
        "message_count": 0,
        "messages": [],
        "project_queries": [],
    }


def _patch_resolve(module: str):
    return patch(
        f"{module}.resolve_system_prompt",
        side_effect=lambda agent_name, **_kw: f"System prompt for {agent_name}.",
    )


def test_returns_healthy_when_no_conversation(db_session: Session, make_project):
    proj = make_project(name="No conversation")
    with patch(f"{_MODULE}.load_aria_conversation_context", return_value=None):
        result = AriaConversationEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_returns_healthy_when_zero_messages(db_session: Session, make_project):
    proj = make_project(name="Zero messages")
    with patch(
        f"{_MODULE}.load_aria_conversation_context",
        return_value=_empty_conversation_context(),
    ):
        result = AriaConversationEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_calls_llm_once_when_messages_present(db_session: Session, make_project):
    proj = make_project(name="Aria conversation LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            f"{_MODULE}.load_aria_conversation_context",
            return_value=_one_conversation_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        _patch_resolve(_MODULE),
    ):
        result = AriaConversationEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_user_prompt_includes_conversation_data(db_session: Session, make_project):
    proj = make_project(name="Aria conversation prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_aria_conversation_context",
            return_value=_one_conversation_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        _patch_resolve(_MODULE),
    ):
        AriaConversationEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert str(proj.id) in prompt
    assert "completed" in prompt


def test_retries_on_invalid_llm_output(db_session: Session, make_project):
    proj = make_project(name="Aria conversation retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            f"{_MODULE}.load_aria_conversation_context",
            return_value=_one_conversation_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        _patch_resolve(_MODULE),
    ):
        result = AriaConversationEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"


def test_both_system_prompts_in_user_prompt(db_session: Session, make_project):
    """Both aria_orchestrator and project_query_agent system prompts appear in the user prompt."""
    proj = make_project(name="Aria dual prompts test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_aria_conversation_context",
            return_value=_one_conversation_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        _patch_resolve(_MODULE),
    ):
        AriaConversationEvaluator().evaluate(db=db_session, project_id=proj.id)

    prompt = captured[0]
    assert "aria_orchestrator" in prompt
    assert "project_query_agent" in prompt


def test_agent_name_is_aria_orchestrator():
    assert AriaConversationEvaluator.AGENT_NAME == "aria_orchestrator"
