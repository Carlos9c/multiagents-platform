from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.schemas.environment import RuntimeEnvironmentPlanOutput
from app.services.environment.contracts import RuntimeSpec
from app.services.environment.planner_client import (
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
        "environment_variables": [],
        "planning_rationale": "Project requires XGBoost for machine learning tasks.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# build_environment_planner_user_prompt
# ---------------------------------------------------------------------------


def test_build_user_prompt_contains_project_info() -> None:
    prompt = build_environment_planner_user_prompt(
        project_name="My ML Project",
        project_description="Train a classification model",
    )
    assert "My ML Project" in prompt
    assert "Train a classification model" in prompt


def test_build_user_prompt_includes_catalog_hint() -> None:
    prompt = build_environment_planner_user_prompt(
        project_name="My ML Project",
        project_description="Train a classification model",
        catalog_hint="python:3.12-slim (Python ML stack)",
    )
    assert "python:3.12-slim" in prompt


def test_build_user_prompt_includes_existing_environment_context() -> None:
    prompt = build_environment_planner_user_prompt(
        project_name="My API",
        project_description="REST API with database",
        existing_environment_context="### requirements.txt\n```\nfastapi==0.115.0\n```",
    )
    assert "requirements.txt" in prompt
    assert "fastapi==0.115.0" in prompt


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
        )

    assert isinstance(result, RuntimeEnvironmentPlanOutput)
    assert result.runtime_type == "python_venv"
    assert result.image == "python:3.12-slim"
    assert len(result.dependencies) == 1
    assert result.dependencies[0].name == "xgboost"
    assert result.dependencies[0].version == "2.1.3"


def test_call_environment_planner_with_existing_context() -> None:
    raw_output = _make_plan_output()

    with patch("app.services.environment.planner_client.get_llm_provider") as mock_factory:
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = raw_output
        mock_factory.return_value = mock_provider

        result = call_environment_planner(
            project_name="ML Project",
            project_description="Train XGBoost model",
            existing_environment_context="### requirements.txt\n```\nnumpy==1.26.0\n```",
        )

    assert isinstance(result, RuntimeEnvironmentPlanOutput)
    assert result.runtime_type == "python_venv"


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
            )


# ---------------------------------------------------------------------------
# plan_runtime_environment service
# ---------------------------------------------------------------------------


def test_plan_runtime_environment_stores_spec(db_session) -> None:
    from app.models.project import Project
    from app.services.environment.catalog.selector_client import CatalogSelectionOutput
    from app.services.environment.planner import plan_runtime_environment

    project = Project(name="Test", description="ML project")
    db_session.add(project)
    db_session.flush()

    raw_output = _make_plan_output()

    no_match = CatalogSelectionOutput(selected_image=None, reasoning="no match")

    with (
        patch("app.services.environment.planner.select_catalog_image", return_value=no_match),
        patch("app.services.environment.planner_client.get_llm_provider") as mock_factory,
    ):
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = raw_output
        mock_factory.return_value = mock_provider

        spec = plan_runtime_environment(db=db_session, project_id=project.id)

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


def test_plan_runtime_environment_with_existing_context(db_session) -> None:
    from app.models.project import Project
    from app.services.environment.catalog.selector_client import CatalogSelectionOutput
    from app.services.environment.planner import plan_runtime_environment

    project = Project(name="Existing API", description="Extend REST API")
    db_session.add(project)
    db_session.flush()

    raw_output = _make_plan_output()
    no_match = CatalogSelectionOutput(selected_image=None, reasoning="no match")

    with (
        patch("app.services.environment.planner.select_catalog_image", return_value=no_match),
        patch("app.services.environment.planner_client.get_llm_provider") as mock_factory,
    ):
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = raw_output
        mock_factory.return_value = mock_provider

        spec = plan_runtime_environment(
            db=db_session,
            project_id=project.id,
            existing_environment_context="### requirements.txt\n```\nfastapi==0.115.0\n```",
        )

    assert isinstance(spec, RuntimeSpec)
    assert spec.runtime_type == "python_venv"
