"""Tests for impact_assessment_agent."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.conversation.impact_assessment_agent import (
    BlockedTaskInfo,
    CompletedTaskSummary,
    ImpactAssessmentError,
    ImpactAssessmentInput,
    PendingTaskSummary,
    assess_impact,
)


def _make_input(**overrides) -> ImpactAssessmentInput:
    defaults = dict(
        project_goal="Build a REST API using FastAPI with SQLite",
        user_clarification="Use PostgreSQL instead of SQLite",
        blocked_task=BlockedTaskInfo(
            task_id=5,
            title="Configure database connection",
            description="Set up SQLite connection pool",
            validation_notes="SQLite is incompatible with concurrent writes in this context",
        ),
        completed_tasks=[
            CompletedTaskSummary(task_id=1, title="Initialize project structure"),
            CompletedTaskSummary(task_id=2, title="Define API routes"),
        ],
        pending_tasks=[
            PendingTaskSummary(
                task_id=6,
                sequence_order=6,
                title="Create user model",
                description="SQLite ORM model",
            ),
            PendingTaskSummary(
                task_id=7,
                sequence_order=7,
                title="Write migration scripts",
                description="SQLite migrations",
            ),
        ],
    )
    defaults.update(overrides)
    return ImpactAssessmentInput(**defaults)


def _raw_narrow() -> dict:
    return {
        "change_scope": "narrow",
        "reasoning": "Only the db connection task needs updating, rest is fine.",
        "tasks_to_modify": [],
        "tasks_to_add": [],
        "resequence_needed": False,
    }


def _raw_moderate() -> dict:
    return {
        "change_scope": "moderate",
        "reasoning": "DB driver changes affect model and migration tasks too.",
        "tasks_to_modify": [
            {
                "task_id": 6,
                "new_description": "PostgreSQL ORM model using asyncpg",
                "new_objective": None,
                "new_implementation_steps": "1. Install psycopg2\n2. Update model",
                "new_acceptance_criteria": "Model maps to Postgres table",
                "new_technical_constraints": "Use psycopg2-binary",
                "reason": "Model was written for SQLite",
            }
        ],
        "tasks_to_add": [
            {
                "insert_after_task_id": 6,
                "title": "Configure connection pool for PostgreSQL",
                "description": "Set up asyncpg pool",
                "objective": "Stable PG connection",
                "proposed_solution": "Use SQLAlchemy async engine",
                "implementation_steps": "1. Install SQLAlchemy[asyncio]\n2. Configure engine",
                "acceptance_criteria": "Pool connects successfully",
                "technical_constraints": "asyncpg>=0.29",
                "reason": "Not previously planned for PG",
            }
        ],
        "resequence_needed": True,
    }


def _raw_disruptive() -> dict:
    return {
        "change_scope": "disruptive",
        "reasoning": "Switching from SQLite to PostgreSQL invalidates the entire data layer design.",
        "tasks_to_modify": [],
        "tasks_to_add": [],
        "resequence_needed": False,
    }


def test_assess_impact_narrow():
    with patch(
        "app.services.conversation.impact_assessment_agent.get_llm_provider"
    ) as mock_factory:
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _raw_narrow()
        mock_factory.return_value = mock_provider

        result = assess_impact(_make_input())

    assert result.change_scope == "narrow"
    assert result.tasks_to_modify == []
    assert result.tasks_to_add == []
    assert result.resequence_needed is False
    assert result.reasoning


def test_assess_impact_moderate():
    with patch(
        "app.services.conversation.impact_assessment_agent.get_llm_provider"
    ) as mock_factory:
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _raw_moderate()
        mock_factory.return_value = mock_provider

        result = assess_impact(_make_input())

    assert result.change_scope == "moderate"
    assert len(result.tasks_to_modify) == 1
    assert result.tasks_to_modify[0].task_id == 6
    assert result.tasks_to_modify[0].new_description == "PostgreSQL ORM model using asyncpg"
    assert len(result.tasks_to_add) == 1
    assert result.tasks_to_add[0].insert_after_task_id == 6
    assert result.resequence_needed is True


def test_assess_impact_disruptive():
    with patch(
        "app.services.conversation.impact_assessment_agent.get_llm_provider"
    ) as mock_factory:
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _raw_disruptive()
        mock_factory.return_value = mock_provider

        result = assess_impact(_make_input())

    assert result.change_scope == "disruptive"
    assert result.tasks_to_modify == []
    assert result.tasks_to_add == []


def test_assess_impact_raises_on_invalid_llm_output():
    with patch(
        "app.services.conversation.impact_assessment_agent.get_llm_provider"
    ) as mock_factory:
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = {"not": "valid"}
        mock_factory.return_value = mock_provider

        with pytest.raises(ImpactAssessmentError):
            assess_impact(_make_input())


def test_assess_impact_with_no_completed_or_pending_tasks():
    inp = _make_input(completed_tasks=[], pending_tasks=[])

    with patch(
        "app.services.conversation.impact_assessment_agent.get_llm_provider"
    ) as mock_factory:
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _raw_narrow()
        mock_factory.return_value = mock_provider

        result = assess_impact(inp)

    assert result.change_scope == "narrow"
