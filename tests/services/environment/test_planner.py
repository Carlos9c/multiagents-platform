from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.schemas.environment import RuntimeEnvironmentPlanOutput
from app.services.environment.contracts import RuntimeSpec
from app.services.environment.planner_client import (
    _format_atomic_tasks,
    build_environment_planner_user_prompt,
    call_environment_planner,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan_output(**overrides) -> dict:
    base = {
        "runtime_type": "python_venv",
        "image": "python:3.12-slim",
        "dependencies": [
            {
                "name": "xgboost",
                "version": "2.1.3",
                "extras": [],
                "purpose": "ML model training",
            }
        ],
        "environment_variables": {},
        "planning_rationale": "Project requires XGBoost for machine learning tasks.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _format_atomic_tasks
# ---------------------------------------------------------------------------


def test_format_atomic_tasks_empty() -> None:
    result = _format_atomic_tasks([])
    assert "No atomic tasks" in result


def test_format_atomic_tasks_includes_key_fields() -> None:
    tasks = [
        {
            "title": "Train model",
            "proposed_solution": "Use XGBoost",
            "technical_constraints": "Must use XGBoost library",
            "tests_required": "pytest tests/",
        }
    ]
    result = _format_atomic_tasks(tasks)
    assert "Train model" in result
    assert "XGBoost" in result
    assert "Must use XGBoost" in result


# ---------------------------------------------------------------------------
# build_environment_planner_user_prompt
# ---------------------------------------------------------------------------


def test_build_user_prompt_contains_project_info() -> None:
    prompt = build_environment_planner_user_prompt(
        project_name="My ML Project",
        project_description="Train a classification model",
        atomic_tasks=[],
    )
    assert "My ML Project" in prompt
    assert "Train a classification model" in prompt


# ---------------------------------------------------------------------------
# call_environment_planner — success path
# ---------------------------------------------------------------------------


def test_call_environment_planner_success() -> None:
    raw_output = _make_plan_output()

    with patch("app.services.environment.planner_client.get_llm_provider") as mock_factory:
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = raw_output
        mock_factory.return_value = mock_provider

        result = call_environment_planner(
            project_name="ML Project",
            project_description="Train XGBoost model",
            atomic_tasks=[],
        )

    assert isinstance(result, RuntimeEnvironmentPlanOutput)
    assert result.runtime_type == "python_venv"
    assert result.image == "python:3.12-slim"
    assert len(result.dependencies) == 1
    assert result.dependencies[0].name == "xgboost"
    assert result.dependencies[0].version == "2.1.3"


def test_call_environment_planner_retry_on_validation_error() -> None:
    invalid_output = {"runtime_type": "python_venv"}
    valid_output = _make_plan_output()

    with patch("app.services.environment.planner_client.get_llm_provider") as mock_factory:
        mock_provider = MagicMock()
        mock_provider.generate_structured.side_effect = [invalid_output, valid_output]
        mock_factory.return_value = mock_provider

        result = call_environment_planner(
            project_name="ML Project",
            project_description="Train XGBoost model",
            atomic_tasks=[],
        )

    assert mock_provider.generate_structured.call_count == 2
    assert result.runtime_type == "python_venv"


def test_call_environment_planner_raises_after_double_invalid() -> None:
    invalid = {"runtime_type": "python_venv"}

    with patch("app.services.environment.planner_client.get_llm_provider") as mock_factory:
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = invalid
        mock_factory.return_value = mock_provider

        with pytest.raises(ValueError):
            call_environment_planner(
                project_name="X",
                project_description="Y",
                atomic_tasks=[],
            )


# ---------------------------------------------------------------------------
# plan_runtime_environment service
# ---------------------------------------------------------------------------


def test_plan_runtime_environment_stores_spec(db_session) -> None:
    from app.models.project import Project
    from app.services.environment.planner import plan_runtime_environment

    project = Project(name="Test", description="ML project")
    db_session.add(project)
    db_session.flush()

    raw_output = _make_plan_output()

    with patch("app.services.environment.planner_client.get_llm_provider") as mock_factory:
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = raw_output
        mock_factory.return_value = mock_provider

        spec = plan_runtime_environment(db=db_session, project_id=project.id, atomic_tasks=[])

    assert isinstance(spec, RuntimeSpec)
    assert spec.runtime_type == "python_venv"

    db_session.refresh(project)
    assert project.runtime_spec is not None
    assert project.runtime_spec["runtime_type"] == "python_venv"
    assert len(project.runtime_spec["change_log"]) == 1
    assert project.runtime_spec["change_log"][0]["change_type"] == "initial"

    # Artifact must be created for traceability
    from app.models.artifact import Artifact

    artifact = (
        db_session.query(Artifact)
        .filter_by(project_id=project.id, artifact_type="runtime_environment_spec")
        .one()
    )
    import json

    artifact_content = json.loads(artifact.content)
    assert artifact_content["runtime_type"] == "python_venv"
    assert artifact_content["image"] == "python:3.12-slim"
    assert artifact.created_by == "environment_planner"
