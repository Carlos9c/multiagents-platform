"""
Tests for ReviewEpisodeEvaluator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators.review_episode_evaluator import (
    ReviewEpisodeEvaluator,
)

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "The review episode pipeline demonstrated appropriate calibration.",
    "issues": [],
    "suggestions": [],
}

_MODULE = "app.services.supervisor.evaluators.review_episode_evaluator"


def _one_episode_context() -> list[dict]:
    return [
        {
            "artifact_id": 55,
            "episode_index": 0,
            "is_project_level": False,
            "blocked_task_id": 42,
            "blocked_task_title": "Implement authentication module",
            "review_attempts": 4,
            "proposed_plan": "Split auth into JWT and session handling.",
            "outcome": "confirmed",
            "impact_scope": "moderate",
            "tasks_modified_count": 2,
            "tasks_added_count": 1,
            "tasks_eliminated_count": 0,
            "tasks_superseded_count": 0,
            "impact_reasoning": "Moderate scope: distinct JWT and session concerns.",
        }
    ]


def _patch_resolve(module: str):
    """Patch resolve_system_prompt to return a deterministic string for any agent."""
    return patch(
        f"{module}.resolve_system_prompt",
        side_effect=lambda agent_name, **_kw: f"System prompt for {agent_name}.",
    )


def test_returns_healthy_when_no_episodes(db_session: Session, make_project):
    proj = make_project(name="No review episodes")
    with patch(f"{_MODULE}.load_review_episode_artifacts", return_value=[]):
        result = ReviewEpisodeEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_calls_llm_once_when_episodes_present(db_session: Session, make_project):
    proj = make_project(name="Review episode LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            f"{_MODULE}.load_review_episode_artifacts",
            return_value=_one_episode_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        _patch_resolve(_MODULE),
    ):
        result = ReviewEpisodeEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_user_prompt_includes_episode_data(db_session: Session, make_project):
    proj = make_project(name="Review episode prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_review_episode_artifacts",
            return_value=_one_episode_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        _patch_resolve(_MODULE),
    ):
        ReviewEpisodeEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert str(proj.id) in prompt
    assert "confirmed" in prompt


def test_retries_on_invalid_llm_output(db_session: Session, make_project):
    proj = make_project(name="Review episode retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            f"{_MODULE}.load_review_episode_artifacts",
            return_value=_one_episode_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        _patch_resolve(_MODULE),
    ):
        result = ReviewEpisodeEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"


def test_three_system_prompts_passed_in_user_prompt(db_session: Session, make_project):
    """All three agent system prompts should appear in the LLM user prompt."""
    proj = make_project(name="Review episode three prompts test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_review_episode_artifacts",
            return_value=_one_episode_context(),
        ),
        patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider),
        _patch_resolve(_MODULE),
    ):
        ReviewEpisodeEvaluator().evaluate(db=db_session, project_id=proj.id)

    prompt = captured[0]
    assert "review_evaluator" in prompt
    assert "confirmation_evaluator" in prompt
    assert "impact_assessment_agent" in prompt


def test_agent_name_is_review_episode():
    assert ReviewEpisodeEvaluator.AGENT_NAME == "review_episode"
