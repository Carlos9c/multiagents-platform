"""Tests for impact_assessment_agent — updated for Phase 3 redesigned output schema."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.conversation.impact_assessment_agent import (
    BlockedTaskInfo,
    CompletedTaskSummary,
    ImpactAssessmentError,
    ImpactAssessmentInput,
    TaskContextForReview,
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
        full_task_tree=[
            TaskContextForReview(
                task_id=5,
                title="Configure database connection",
                description="Set up SQLite connection pool",
                planning_level="atomic",
                parent_task_id=10,
                parent_title="Database layer",
                status="awaiting_review",
                depends_on_task_titles=[],
                acceptance_criteria="Connection pool initialized",
                sequence_order=5,
            ),
            TaskContextForReview(
                task_id=6,
                title="Create user model",
                description="SQLite ORM model",
                planning_level="atomic",
                parent_task_id=10,
                parent_title="Database layer",
                status="pending",
                depends_on_task_titles=["Configure database connection"],
                acceptance_criteria="User model persists to DB",
                sequence_order=6,
            ),
            TaskContextForReview(
                task_id=7,
                title="Write migration scripts",
                description="SQLite migrations",
                planning_level="atomic",
                parent_task_id=10,
                parent_title="Database layer",
                status="pending",
                depends_on_task_titles=["Create user model"],
                acceptance_criteria="Migrations run cleanly",
                sequence_order=7,
            ),
            TaskContextForReview(
                task_id=10,
                title="Database layer",
                description="All DB-related tasks",
                planning_level="high_level",
                parent_task_id=None,
                parent_title=None,
                status="pending",
                depends_on_task_titles=[],
                acceptance_criteria=None,
                sequence_order=None,
            ),
        ],
    )
    defaults.update(overrides)
    return ImpactAssessmentInput(**defaults)


def _raw_narrow() -> dict:
    return {
        "change_scope": "narrow",
        "reasoning": "Only the db connection task needs updating, rest is fine.",
        "tasks_to_eliminate": [],
        "tasks_to_modify": [],
        "new_work_blocks": [],
        "environment_changes": [],
        "updated_project_goal": None,
    }


def _raw_moderate() -> dict:
    return {
        "change_scope": "moderate",
        "reasoning": "DB driver changes affect model and migration tasks too.",
        "tasks_to_eliminate": [],
        "tasks_to_modify": [
            {
                "task_id": 6,
                "new_description": "PostgreSQL ORM model using asyncpg",
                "new_objective": None,
                "new_implementation_steps": "1. Install psycopg2\n2. Update model",
                "new_acceptance_criteria": "Model maps to Postgres table",
                "new_technical_constraints": "Use psycopg2-binary",
                "new_depends_on_task_titles": None,
                "reason": "Model was written for SQLite",
            }
        ],
        "new_work_blocks": [
            {
                "title": "Configure connection pool for PostgreSQL",
                "description": "Set up asyncpg pool",
                "objective": "Stable PG connection",
                "proposed_solution": "Use SQLAlchemy async engine",
                "acceptance_criteria": "Pool connects successfully",
                "technical_constraints": "asyncpg>=0.29",
                "out_of_scope": "Non-PostgreSQL databases",
                "task_type": "implementation",
                "depends_on_task_titles": ["Create user model"],
                "planning_level": "atomic",
                "parent_task_id": 10,
                "reason": "Not previously planned for PG",
            }
        ],
        "environment_changes": [],
        "updated_project_goal": None,
    }


def _raw_moderate_with_elimination() -> dict:
    return {
        "change_scope": "moderate",
        "reasoning": "User wants to skip Redis and use DB sessions instead.",
        "tasks_to_eliminate": [6, 7],
        "tasks_to_modify": [],
        "new_work_blocks": [
            {
                "title": "DB-based session auth",
                "description": "Replace Redis sessions with DB-backed sessions",
                "objective": "Session persistence without Redis",
                "proposed_solution": "Use SQLAlchemy session table",
                "acceptance_criteria": "Sessions survive server restart",
                "technical_constraints": "PostgreSQL only",
                "out_of_scope": "Redis",
                "task_type": "implementation",
                "depends_on_task_titles": [],
                "planning_level": "atomic",
                "parent_task_id": 10,
                "reason": "User eliminated Redis, needs DB-based alternative",
            }
        ],
        "environment_changes": [],
        "updated_project_goal": None,
    }


def _raw_disruptive() -> dict:
    return {
        "change_scope": "disruptive",
        "reasoning": "Switching from SQLite to PostgreSQL invalidates the entire data layer design.",
        "tasks_to_eliminate": [],
        "tasks_to_modify": [],
        "new_work_blocks": [],
        "environment_changes": [],
        "updated_project_goal": "Build a REST API using FastAPI with PostgreSQL",
    }


def _patch_llm(raw: dict):
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = raw
    return patch(
        "app.services.conversation.impact_assessment_agent.get_llm_provider",
        return_value=mock_provider,
    )


def test_assess_impact_narrow():
    with _patch_llm(_raw_narrow()):
        result = assess_impact(_make_input())

    assert result.change_scope == "narrow"
    assert result.tasks_to_eliminate == []
    assert result.tasks_to_modify == []
    assert result.new_work_blocks == []
    assert result.reasoning


def test_assess_impact_moderate_with_modifications():
    with _patch_llm(_raw_moderate()):
        result = assess_impact(_make_input())

    assert result.change_scope == "moderate"
    assert result.tasks_to_eliminate == []
    assert len(result.tasks_to_modify) == 1
    assert result.tasks_to_modify[0].task_id == 6
    assert result.tasks_to_modify[0].new_description == "PostgreSQL ORM model using asyncpg"
    assert result.tasks_to_modify[0].new_depends_on_task_titles is None
    assert len(result.new_work_blocks) == 1
    assert result.new_work_blocks[0].planning_level == "atomic"
    assert result.new_work_blocks[0].parent_task_id == 10


def test_assess_impact_moderate_with_elimination():
    with _patch_llm(_raw_moderate_with_elimination()):
        result = assess_impact(_make_input())

    assert result.change_scope == "moderate"
    assert result.tasks_to_eliminate == [6, 7]
    assert result.tasks_to_modify == []
    assert len(result.new_work_blocks) == 1
    assert result.new_work_blocks[0].title == "DB-based session auth"


def test_assess_impact_disruptive():
    with _patch_llm(_raw_disruptive()):
        result = assess_impact(_make_input())

    assert result.change_scope == "disruptive"
    assert result.tasks_to_eliminate == []
    assert result.new_work_blocks == []
    assert result.updated_project_goal == "Build a REST API using FastAPI with PostgreSQL"


def test_assess_impact_raises_on_invalid_llm_output():
    with _patch_llm({"not": "valid"}):
        with pytest.raises(ImpactAssessmentError):
            assess_impact(_make_input())


def test_assess_impact_with_no_completed_or_pending_tasks():
    inp = _make_input(completed_tasks=[], full_task_tree=[])

    with _patch_llm(_raw_narrow()):
        result = assess_impact(inp)

    assert result.change_scope == "narrow"


def test_assess_impact_full_task_tree_passed_to_prompt():
    """Verify the full_task_tree is serialised into the user prompt."""
    captured: list[str] = []

    mock_provider = MagicMock()

    def _capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _raw_narrow()

    mock_provider.generate_structured.side_effect = _capture

    with patch(
        "app.services.conversation.impact_assessment_agent.get_llm_provider",
        return_value=mock_provider,
    ):
        assess_impact(_make_input())

    assert captured, "generate_structured was not called"
    prompt = captured[0]
    # Task titles from the tree must appear in the prompt
    assert "Configure database connection" in prompt
    assert "Create user model" in prompt
    assert "PENDING TASK TREE" in prompt


def test_assess_impact_environment_changes_propagated():
    raw = {
        **_raw_narrow(),
        "environment_changes": [
            {
                "package_name": "psycopg2-binary",
                "version_constraint": None,
                "reason": "PostgreSQL driver",
                "version_strictness": "any_compatible",
            }
        ],
    }
    with _patch_llm(raw):
        result = assess_impact(_make_input())

    assert len(result.environment_changes) == 1
    assert result.environment_changes[0].package_name == "psycopg2-binary"


def test_new_work_block_high_level_no_parent():
    raw = {
        **_raw_narrow(),
        "change_scope": "moderate",
        "new_work_blocks": [
            {
                "title": "New caching feature",
                "description": "Introduce Memcached as caching layer",
                "objective": "Reduce DB pressure",
                "proposed_solution": "pymemcache client",
                "acceptance_criteria": "Cache hit > 50%",
                "technical_constraints": "Memcached 1.6+",
                "out_of_scope": "Redis",
                "task_type": "implementation",
                "depends_on_task_titles": [],
                "planning_level": "high_level",
                "parent_task_id": None,
                "reason": "New feature area",
            }
        ],
    }
    with _patch_llm(raw):
        result = assess_impact(_make_input())

    assert result.new_work_blocks[0].planning_level == "high_level"
    assert result.new_work_blocks[0].parent_task_id is None
